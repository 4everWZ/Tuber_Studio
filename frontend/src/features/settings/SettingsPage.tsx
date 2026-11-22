import { ArtworkReferencesPanel, CharacterDisplayPanel } from "./ArtworkReferencesPanel";
import { IllustrationPolicyPanel } from "./IllustrationPolicyPanel";
import { ProvidersPanel } from "./ProvidersPanel";

export function SettingsPage() {
  return (
    <section data-section="settings" className="win-page-view">
      <div className="page-heading win-heading">
        <h1>工坊设置</h1>
        <a href="#voices" className="win-link-btn">
          角色音色试听 →
        </a>
      </div>

      <CharacterDisplayPanel />
      <IllustrationPolicyPanel />
      <ArtworkReferencesPanel />
      <ProvidersPanel />
    </section>
  );
}
