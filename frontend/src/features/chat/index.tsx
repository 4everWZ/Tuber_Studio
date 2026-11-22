import type { ParsedRoute } from "@/shell/navigation";
import { ChatPage } from "./ChatPage";

export function ChatFeature({ route: _route }: { route: ParsedRoute }) {
  return <ChatPage />;
}
