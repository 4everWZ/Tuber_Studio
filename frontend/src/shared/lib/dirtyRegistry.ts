type DirtyCheck = () => boolean;

const registry = new Map<string, DirtyCheck>();

export function registerDirtySource(key: string, check: DirtyCheck): () => void {
  registry.set(key, check);
  return () => {
    if (registry.get(key) === check) {
      registry.delete(key);
    }
  };
}

export function hasAnyUnsavedChanges(): boolean {
  for (const check of registry.values()) {
    try {
      if (check()) return true;
    } catch {
      // Ignore disposed checkers
    }
  }
  return false;
}

export function clearDirtyRegistry(): void {
  registry.clear();
}
