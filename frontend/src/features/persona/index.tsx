import { PAGE_TITLES, type ParsedRoute } from "@/shell/navigation";

export function PersonaFeature({ route }: { route: ParsedRoute }) {
  return (
    <section data-section={route.page} className="win-page-view">
      <div className="page-heading win-heading">
        <h1>{PAGE_TITLES[route.page]}</h1>
      </div>
      <div className="panel">
        <p className="hint">角色工作室与个性化占位视图（M4-A 脚手架就绪）。</p>
      </div>
    </section>
  );
}
