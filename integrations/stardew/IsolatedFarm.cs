using System;
using System.IO;
using System.Linq;
using System.Text.Json;
using StardewModdingAPI;
using StardewModdingAPI.Events;
using StardewValley;
using StardewValley.Tools;
using Microsoft.Xna.Framework;

namespace TuberStudio;

public sealed class IsolatedFarm : Mod
{
    private readonly string session = Guid.NewGuid().ToString();
    private string bridge, inbox;
    private FileStream bridgeLock;
    private string owner = "operator";
    private int epoch;
    private long leaseUntil;
    private Command pending;
    private int expected;
    private string before;
    private static long Now => DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();

    public sealed record Command(string session, string id, int epoch, string action, long expires, string target, string item);

    private static string FrontKey(Vector2 tile) =>
        $"{Game1.player.currentLocation.NameOrUniqueName}:{(int)tile.X}:{(int)tile.Y}";

    private static Vector2 FrontTile() => Game1.player.Tile + (Game1.player.FacingDirection switch {
        0 => new Vector2(0, -1), 1 => new Vector2(1, 0), 2 => new Vector2(0, 1), _ => new Vector2(-1, 0)
    });

    public override void Entry(IModHelper helper)
    {
        bridge = Environment.GetEnvironmentVariable("TUBER_STARDEW_BRIDGE");
        string profile = Environment.GetEnvironmentVariable("TUBER_STARDEW_PROFILE");
        if (!Path.IsPathFullyQualified(bridge ?? ".") || !Path.IsPathFullyQualified(profile ?? ".") ||
            !File.Exists(Path.Combine(Constants.GamePath, ".tuber-task-copy")) ||
            Path.GetDirectoryName(Path.GetFullPath(profile)) != Path.GetDirectoryName(Constants.GamePath) ||
            Path.GetDirectoryName(Path.GetFullPath(bridge)) != Path.GetDirectoryName(Constants.GamePath) ||
            !Path.GetFullPath(Constants.SavesPath).StartsWith(Path.GetFullPath(profile) + Path.DirectorySeparatorChar,
                                                             StringComparison.Ordinal))
        {
            Monitor.Log("Isolated profile or bridge unavailable. Controls disabled.", LogLevel.Warn);
            return;
        }
        Directory.CreateDirectory(bridge);
        try { bridgeLock = new FileStream(Path.Combine(bridge, "device.lock"), FileMode.OpenOrCreate,
                                          FileAccess.ReadWrite, FileShare.None); }
        catch (IOException) { Monitor.Log("Isolated bridge already in use.", LogLevel.Warn); return; }
        inbox = Directory.CreateDirectory(Path.Combine(bridge, session)).FullName;
        helper.Events.Input.ButtonPressed += (_, e) => {
            if (e.Button == SButton.F8) Takeover();
        };
        helper.Events.GameLoop.ReturnedToTitle += (_, _) => Takeover();
        helper.Events.GameLoop.UpdateTicked += Tick;
    }

    private static void Write(string path, object data)
    {
        string temporary = path + ".tmp";
        File.WriteAllText(temporary, JsonSerializer.Serialize(data));
        File.Move(temporary, path, true);
    }

    private void Takeover()
    {
        owner = "operator";
        epoch++;
        leaseUntil = 0;
        if (pending != null)
            Finish(pending, "unknown", "Control changed before readback.");
        Snapshot();
    }

    private object Farm()
    {
        if (!Context.IsWorldReady) return null;
        var p = Game1.player;
        var tile = FrontTile();
        var soil = p.currentLocation.GetHoeDirtAtTile(tile);
        return new {
            save = Path.GetFileName(Constants.CurrentSavePath), day = Game1.dayOfMonth,
            season = Game1.currentSeason, time = Game1.timeOfDay, location = p.currentLocation.NameOrUniqueName,
            stamina = p.Stamina, money = p.Money, x = p.TilePoint.X, y = p.TilePoint.Y,
            facing = p.FacingDirection, slot = p.CurrentToolIndex,
            front = new { key = FrontKey(tile), tilled = soil != null, planted = soil?.crop != null,
                          watered = soil?.isWatered() ?? false },
            inventory = p.Items.Select(i => i == null ? null : new { item = i.QualifiedItemId, count = i.Stack }).ToArray()
        };
    }

    private void Snapshot() => Write(Path.Combine(bridge, "state.json"), new {
        session, owner, epoch, observed = Now, kind = "device", game = "stardew_valley",
        version = Game1.version, can_act = Context.IsPlayerFree && !Context.IsMultiplayer, farm = Farm()
    });

    private void Finish(Command cmd, string status, string reason)
    {
        Snapshot();
        Write(Path.Combine(inbox, cmd.id + ".result"), new {
            session, cmd.id, cmd.action, status, reason, owner, epoch, before, farm = Farm(), observed = Now
        });
        pending = null;
        before = null;
    }

    private void Tick(object sender, UpdateTickedEventArgs e)
    {
        if (!e.IsMultipleOf(6)) return;
        if (owner == "studio" && Now >= leaseUntil) Takeover();
        if (pending != null)
        {
            bool ok = Context.IsWorldReady && Readback(pending);
            Finish(pending, ok ? "succeeded" : "unknown", ok ? "Observed on subsequent game tick." : "State changed.");
        }
        if (e.IsMultipleOf(30)) Snapshot();
        // Takeover has priority over queued work. The epoch invalidates all earlier commands.
        var paths = Directory.EnumerateFiles(inbox, "*.request")
            .OrderByDescending(p => Path.GetFileName(p).StartsWith("takeover-"));
        foreach (string path in paths)
        {
            string claimed = Path.ChangeExtension(path, ".claimed");
            try { File.Move(path, claimed); }
            catch (FileNotFoundException) { continue; } // cancelled before the game claimed it
            Command cmd;
            try { cmd = JsonSerializer.Deserialize<Command>(File.ReadAllText(claimed)); }
            catch (JsonException) { continue; }
            if (cmd == null || cmd.session != session || !Guid.TryParseExact(cmd.id, "D", out _) ||
                File.Exists(Path.Combine(inbox, cmd.id + ".result"))) continue;
            if (cmd.expires < Now || (cmd.action != "takeover" && cmd.epoch != epoch))
            { Finish(cmd, "rejected", "Expired command or control changed."); continue; }
            if (cmd.action == "takeover") { Takeover(); Finish(cmd, "succeeded", "Operator has control."); continue; }
            if (cmd.action == "release")
            {
                owner = "studio"; epoch++; leaseUntil = Now + 30000;
                Finish(cmd, "succeeded", "Studio control granted for 30 seconds."); continue;
            }
            if (owner != "studio" || !Context.IsPlayerFree || !Context.IsWorldReady || Context.IsMultiplayer)
            { Finish(cmd, "rejected", "Operator control or player unavailable."); continue; }
            var player = Game1.player;
            if (cmd.action == "till_front" || cmd.action == "plant_front" || cmd.action == "water_front")
            {
                before = JsonSerializer.Serialize(Farm());
                if (!WorkFront(cmd))
                { Finish(cmd, "rejected", "Target, soil, equipped item or stamina changed."); continue; }
            }
            else if (cmd.action == "next_slot")
            {
                before = player.CurrentToolIndex.ToString();
                expected = (player.CurrentToolIndex + 1) % Math.Min(12, player.MaxItems);
                player.CurrentToolIndex = expected;
            }
            else if (cmd.action == "turn_right")
            {
                before = player.FacingDirection.ToString();
                expected = (player.FacingDirection + 1) % 4;
                player.faceDirection(expected);
            }
            else { Finish(cmd, "rejected", "Unsupported action."); continue; }
            leaseUntil = Now + 30000;
            pending = cmd;
            break;
        }
    }

    private bool Readback(Command cmd)
    {
        var p = Game1.player;
        if (cmd.action == "next_slot") return p.CurrentToolIndex == expected;
        if (cmd.action == "turn_right") return p.FacingDirection == expected;
        var tile = FrontTile();
        var soil = p.currentLocation.GetHoeDirtAtTile(tile);
        return FrontKey(tile) == cmd.target && soil != null && (cmd.action == "till_front" ||
            (cmd.action == "plant_front" && soil.crop != null) || (cmd.action == "water_front" && soil.isWatered()));
    }

    private static bool WorkFront(Command cmd)
    {
        var p = Game1.player;
        var map = p.currentLocation;
        var tile = FrontTile();
        int x = (int)tile.X, y = (int)tile.Y;
        if (!(map is Farm) || cmd.target != FrontKey(tile) || cmd.item != p.CurrentItem?.QualifiedItemId ||
            p.Stamina < 3) return false;
        var soil = map.GetHoeDirtAtTile(tile);
        if (cmd.action == "plant_front")
        {
            if (soil == null || soil.crop != null || p.CurrentItem is not StardewValley.Object seed ||
                seed.Category != StardewValley.Object.SeedsCategory || !soil.plant(seed.ItemId, p, false)) return false;
            p.reduceActiveItemByOne();
            return true;
        }
        if (cmd.action == "till_front" && p.CurrentTool is Hoe && soil == null &&
            map.doesTileHaveProperty(x, y, "Diggable", "Back") != null && map.CanItemBePlacedHere(tile) ||
            cmd.action == "water_front" && p.CurrentTool is WateringCan can && can.WaterLeft > 0 &&
            soil != null && !soil.isWatered())
        {
            p.CurrentTool.DoFunction(map, x * 64 + 32, y * 64 + 32, 0, p);
            return true;
        }
        return false;
    }
}
