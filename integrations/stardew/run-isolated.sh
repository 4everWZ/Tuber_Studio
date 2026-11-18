#!/usr/bin/env bash
set -eu
root=$(realpath "${1:?Specify the prepared task directory}")
scripts=$(cd "$(dirname "$0")" && pwd)
test "$(uname -s)" = Linux
test -f "$root/game/.tuber-task-copy"
test -x "$root/dotnet/dotnet"
test -f "$root/studio-mods/IsolatedFarm/IsolatedFarm.dll"
for dir in game studio-mods profile bridge profile/config profile/local; do
    test "$(realpath -m "$root/$dir")" = "$root/$dir"
done
export TUBER_STARDEW_PROFILE="$root/profile" TUBER_STARDEW_BRIDGE="$root/bridge"
export XDG_CONFIG_HOME="$root/profile/config" XDG_DATA_HOME="$root/profile/local"
mkdir -p "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$TUBER_STARDEW_BRIDGE"
cd "$root/game"
export LD_LIBRARY_PATH="$root/game" LP_NUM_THREADS=2
exec "$root/dotnet/dotnet" exec --runtimeconfig "$scripts/host.runtimeconfig.json" StardewModdingAPI.dll \
    --mods-path "$root/studio-mods" --no-terminal
