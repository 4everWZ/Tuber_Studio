import { PAGE_TITLES, type ParsedRoute } from "@/shell/navigation";

export function LiveFeature({ route }: { route: ParsedRoute }) {
  return (
    <section data-section={route.page} className="win-page-view">
      <div className="page-heading win-heading">
        <h1>{PAGE_TITLES[route.page]}</h1>
      </div>
      <div className="panel">
        <p className="hint">直播工作台占位视图（M4-A 脚手架就绪）。</p>
      </div>
    </section>
  );
}

export function LiveStandaloneApp() {
  return (
    <main className="panel">
      <h1>直播工作台</h1>
      <p className="hint">独立直播入口占位视图（M4-A 脚手架就绪）。</p>
    </main>
  );
}

export function OverlayPlayerApp() {
  return <div id="scene" data-overlay-placeholder="ready" />;
}

export function DrawGuessOverlayApp() {
  return <div id="draw-guess-container" hidden />;
}
