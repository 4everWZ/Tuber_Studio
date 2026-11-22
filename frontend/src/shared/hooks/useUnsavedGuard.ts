import { useEffect, useRef } from "react";
import { registerDirtySource } from "../lib/dirtyRegistry";

/** Register a component/form's unsaved state in the global `beforeunload` guard. */
export function useUnsavedGuard(id: string, isDirty: boolean | (() => boolean)): void {
  const checkRef = useRef<() => boolean>(() => false);
  checkRef.current = typeof isDirty === "function" ? isDirty : () => isDirty;

  useEffect(() => {
    return registerDirtySource(id, () => checkRef.current());
  }, [id]);
}
