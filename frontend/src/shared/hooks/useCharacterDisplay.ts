import { useSyncExternalStore } from "react";
import { apiGet, apiPut } from "../api/client";

export interface CharacterDisplaySettings {
  pets_enabled: boolean;
  avatar_enabled: boolean;
}

export interface CharacterDisplaySnapshot {
  value: CharacterDisplaySettings | null;
  saving: boolean;
}

const PERFORMANCE_PATH = "/api/v1/system/settings/performance";

function isValidDisplay(data: unknown): data is CharacterDisplaySettings {
  return (
    typeof data === "object" &&
    data !== null &&
    typeof (data as CharacterDisplaySettings).pets_enabled === "boolean" &&
    typeof (data as CharacterDisplaySettings).avatar_enabled === "boolean"
  );
}

export class CharacterDisplayStore {
  private value: CharacterDisplaySettings | null = null;
  private saving = false;
  private revision = 0;
  private listeners = new Set<() => void>();
  private snapshot: CharacterDisplaySnapshot = { value: null, saving: false };

  private emit(): void {
    this.snapshot = { value: this.value, saving: this.saving };
    for (const listener of this.listeners) {
      listener();
    }
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  getSnapshot = (): CharacterDisplaySnapshot => this.snapshot;

  publish(next: unknown): CharacterDisplaySettings {
    if (!isValidDisplay(next)) {
      throw new Error("无法读取角色显示设置");
    }
    this.value = next;
    this.emit();
    return next;
  }

  reset(): void {
    this.revision += 1;
    this.value = null;
    this.saving = false;
    this.emit();
  }

  async refresh(): Promise<CharacterDisplaySettings | undefined> {
    if (this.saving) return undefined;
    const revision = ++this.revision;
    const data = await apiGet<typeof PERFORMANCE_PATH, CharacterDisplaySettings>(PERFORMANCE_PATH);
    if (revision === this.revision) {
      return this.publish(data);
    }
    return undefined;
  }

  async save(key: keyof CharacterDisplaySettings, enabled: boolean): Promise<CharacterDisplaySettings | undefined> {
    if (this.saving || !this.value) return undefined;
    const revision = ++this.revision;
    this.saving = true;
    this.emit();
    try {
      const data = await apiPut<typeof PERFORMANCE_PATH, CharacterDisplaySettings, Partial<CharacterDisplaySettings>>(
        PERFORMANCE_PATH,
        { body: { [key]: enabled } }
      );
      if (revision === this.revision) {
        return this.publish(data);
      }
      return undefined;
    } finally {
      if (revision === this.revision) {
        this.saving = false;
        this.emit();
      }
    }
  }
}

export const characterDisplayStore = new CharacterDisplayStore();

export function useCharacterDisplay() {
  const snapshot = useSyncExternalStore(
    characterDisplayStore.subscribe,
    characterDisplayStore.getSnapshot,
    characterDisplayStore.getSnapshot
  );

  return {
    value: snapshot.value,
    saving: snapshot.saving,
    refresh: () => characterDisplayStore.refresh(),
    save: (key: keyof CharacterDisplaySettings, enabled: boolean) => characterDisplayStore.save(key, enabled),
    reset: () => characterDisplayStore.reset(),
  };
}
