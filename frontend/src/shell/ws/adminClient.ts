import { studioWsUrl } from "@/shared/api/client";
import type { ContextStatusSnapshot, SystemStatus } from "@/shared/hooks/useAppSession";

export interface AdminWsHandlers {
  onStatus: (status: SystemStatus) => void;
  onContextUpdated?: (context: ContextStatusSnapshot) => void;
  onConnectionChange?: (connected: boolean) => void;
  onUnauthorized?: () => void;
}

const AUTH_CLOSE_CODES = new Set([1008, 4401, 4403]);

export class AdminWsClient {
  private socket: WebSocket | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private closed = true;
  private attempts = 0;
  private readonly handlers: AdminWsHandlers;

  constructor(handlers: AdminWsHandlers) {
    this.handlers = handlers;
  }

  get isOpen(): boolean {
    return typeof WebSocket !== "undefined" && this.socket?.readyState === WebSocket.OPEN;
  }

  connect(): void {
    this.closed = false;
    this.clearReconnect();
    this.closeSocket();

    if (typeof WebSocket === "undefined") return;

    const socket = new WebSocket(studioWsUrl("/ws/admin"));
    this.socket = socket;

    socket.onopen = () => {
      if (this.socket !== socket || this.closed) return;
      this.attempts = 0;
      this.handlers.onConnectionChange?.(true);
    };

    socket.onmessage = (event) => {
      if (this.socket !== socket || this.closed) return;
      try {
        const message = JSON.parse(String(event.data)) as {
          type?: string;
          data?: SystemStatus;
          context?: ContextStatusSnapshot;
        };
        if (message.type === "status" && message.data) {
          this.handlers.onStatus(message.data);
        } else if (message.type === "context_updated" && message.context) {
          this.handlers.onContextUpdated?.(message.context);
        }
      } catch {
        // Ignore malformed socket frames
      }
    };

    socket.onerror = () => {
      if (this.socket !== socket) return;
      this.handlers.onConnectionChange?.(false);
    };

    socket.onclose = (event) => {
      if (this.socket !== socket) return;
      this.socket = null;
      this.handlers.onConnectionChange?.(false);
      if (this.closed) return;
      if (AUTH_CLOSE_CODES.has(event.code)) {
        this.closed = true;
        this.handlers.onUnauthorized?.();
        return;
      }
      const delay = Math.min(10000, 1000 * 2 ** Math.min(this.attempts, 3));
      this.attempts += 1;
      this.reconnectTimer = setTimeout(() => {
        if (!this.closed) this.connect();
      }, delay);
    };
  }

  disconnect(): void {
    this.closed = true;
    this.clearReconnect();
    this.closeSocket();
    this.handlers.onConnectionChange?.(false);
  }

  private clearReconnect(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private closeSocket(): void {
    if (this.socket) {
      const existing = this.socket;
      this.socket = null;
      existing.onopen = null;
      existing.onmessage = null;
      existing.onerror = null;
      existing.onclose = null;
      existing.close();
    }
  }
}
