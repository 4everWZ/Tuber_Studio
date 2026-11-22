import type { ParsedRoute } from "@/shell/navigation";

export function ChatFeature({ route: _route }: { route: ParsedRoute }) {
  return (
    <section data-section="chat" className="operator-chat">
      <div className="panel">
        <h2>对话</h2>
        <p className="hint">Chats 工作区占位视图（M4-A 脚手架就绪）。</p>
      </div>
    </section>
  );
}
