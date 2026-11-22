import { useEffect, useRef, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { useCharacterDisplay } from "@/shared/hooks/useCharacterDisplay";
import { errorMessage } from "@/shared/lib/errors";
import type { PetCharacterInfo } from "./types";

export function focusMenuButtonByKey(
  panel: HTMLElement | null,
  event: ReactKeyboardEvent,
  onClose: () => void,
  toggleEl: HTMLElement | null
): void {
  if (event.key === "Escape") {
    event.preventDefault();
    onClose();
    toggleEl?.focus({ preventScroll: true });
    return;
  }
  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key) || !panel) return;
  const items = Array.from(panel.querySelectorAll<HTMLButtonElement>("button:not(:disabled)"));
  if (!items.length) return;
  event.preventDefault();
  const current = items.indexOf(document.activeElement as HTMLButtonElement);
  const index =
    event.key === "Home"
      ? 0
      : event.key === "End"
        ? items.length - 1
        : (current + items.length + (event.key === "ArrowDown" ? 1 : -1)) % items.length;
  items[index].focus({ preventScroll: true });
}

export interface ChatHeaderMenuProps {
  open: boolean;
  pending: boolean;
  character: PetCharacterInfo | null;
  onToggleOpen: () => void;
  onClose: () => void;
  onOpenTemporaryChat: () => void;
  onClearChat: () => void;
  onNotify: (message: string, isError?: boolean) => void;
}

export function ChatHeaderMenu({
  open,
  pending,
  character,
  onToggleOpen,
  onClose,
  onOpenTemporaryChat,
  onClearChat,
  onNotify,
}: ChatHeaderMenuProps) {
  const display = useCharacterDisplay();
  const popupRef = useRef<HTMLDivElement | null>(null);
  const toggleRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (open) {
      panelRef.current?.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus({
        preventScroll: true,
      });
    }
  }, [open]);

  useEffect(() => {
    if (!open || typeof document === "undefined") return;
    const onPointerOrFocus = (event: Event) => {
      if (popupRef.current && !popupRef.current.contains(event.target as Node)) {
        onClose();
      }
    };
    document.addEventListener("pointerdown", onPointerOrFocus);
    document.addEventListener("focusin", onPointerOrFocus);
    return () => {
      document.removeEventListener("pointerdown", onPointerOrFocus);
      document.removeEventListener("focusin", onPointerOrFocus);
    };
  }, [open, onClose]);

  const petHidden = !display.value?.pets_enabled;
  const petToggleDisabled = !character || !display.value || display.saving;

  async function handleTogglePet() {
    if (!display.value) return;
    try {
      await display.save("pets_enabled", !display.value.pets_enabled);
    } catch (error) {
      onNotify(errorMessage(error), true);
    }
    onClose();
    toggleRef.current?.focus({ preventScroll: true });
  }

  return (
    <div
      ref={popupRef}
      id="operator-chat-more"
      className="operator-chat-popup"
      onKeyDown={(event) => {
        if (open) focusMenuButtonByKey(panelRef.current, event, onClose, toggleRef.current);
      }}
    >
      <button
        ref={toggleRef}
        id="operator-chat-more-toggle"
        className="operator-chat-icon"
        type="button"
        aria-label="更多对话操作"
        aria-expanded={open}
        aria-controls="operator-chat-more-panel"
        disabled={pending}
        onClick={onToggleOpen}
      >
        <span aria-hidden="true">···</span>
      </button>
      <div
        ref={panelRef}
        id="operator-chat-more-panel"
        className="operator-chat-menu operator-chat-menu-end"
        hidden={!open}
      >
        <button
          id="chat-pet-toggle"
          type="button"
          disabled={petToggleDisabled}
          onClick={() => void handleTogglePet()}
        >
          {petHidden ? "显示角色宠物" : "隐藏角色宠物"}
        </button>
        <button
          id="temporary-chat-open"
          type="button"
          onClick={() => {
            onClose();
            onOpenTemporaryChat();
          }}
        >
          临时对话
        </button>
        <button
          id="operator-chat-clear"
          type="button"
          disabled={pending}
          onClick={() => {
            onClose();
            onClearChat();
          }}
        >
          清空私聊
        </button>
      </div>
    </div>
  );
}
