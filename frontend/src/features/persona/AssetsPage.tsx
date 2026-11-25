import { useEffect, useState } from "react";
import { apiGet, studioPath } from "@/shared/api/client";
import { display, list } from "@/shared/lib/display";
import { emitNotice, errorMessage } from "@/shared/lib/errors";
import { StatusPill } from "@/shared/ui/StatusPill";
import { MemeGallery } from "./MemeGallery";
import styles from "./MemeGallery.module.css";
import type { PersonaAssetItem } from "./types";

function AssetPreview({ asset }: { asset: PersonaAssetItem }) {
  const [imgFailed, setImgFailed] = useState(false);
  const rawUrl = asset.url || asset.path || "";
  const isAudio =
    /\.(wav|mp3|ogg|flac)(?:\?|$)/i.test(rawUrl) || asset.kind === "audio" || asset.type === "audio";

  if (isAudio) {
    return (
      <div className={`asset-preview ${styles.assetPreview}`}>
        <audio controls preload="none" src={studioPath(rawUrl)} />
      </div>
    );
  }

  return (
    <div className={`asset-preview ${styles.assetPreview}`}>
      {!imgFailed && rawUrl ? (
        <img
          src={studioPath(rawUrl)}
          alt={asset.tag || asset.event_tag || asset.id}
          loading="lazy"
          onError={() => setImgFailed(true)}
        />
      ) : (
        <span className="muted">素材不可用</span>
      )}
    </div>
  );
}

export function AssetsPage() {
  const [assets, setAssets] = useState<PersonaAssetItem[]>([]);

  useEffect(() => {
    let mounted = true;
    apiGet<"/api/v1/persona/assets", PersonaAssetItem[]>("/api/v1/persona/assets")
      .then((data) => {
        if (mounted) setAssets(list<PersonaAssetItem>(data));
      })
      .catch((error) => {
        if (mounted) emitNotice(errorMessage(error), true);
      });
    return () => {
      mounted = false;
    };
  }, []);

  return (
    <section data-section="assets" className="win-page-view">
      <div className="page-heading win-heading">
        <div className="heading-title-box">
          <p className="eyebrow">角色动态表情与预录音效库</p>
          <h1>素材与表情包图库</h1>
        </div>
        <a href="#cache-panel" className="win-link-btn">
          媒体与资产缓存 →
        </a>
      </div>

      <div className="win-group-box">
        <div className="win-group-title">动态配图与表情包资产库</div>
        <div id="meme-gallery">
          <MemeGallery />
        </div>
      </div>

      <div className={`win-group-box ${styles.assetSection}`}>
        <div className="win-group-title">角色多媒体素材清单</div>
        <div id="asset-grid" className={`asset-grid ${styles.assetGrid}`}>
          {assets.length === 0 ? (
            <p className="muted">暂无已配置素材。</p>
          ) : (
            assets.map((asset) => {
              const source = asset.source || asset.source_url;
              return (
                <article key={asset.id} className={`asset-card ${styles.assetCard}`}>
                  <AssetPreview asset={asset} />
                  <div className={`asset-meta ${styles.assetMeta}`}>
                    <h3>{asset.id}</h3>
                    <StatusPill label={asset.tag || asset.event_tag || asset.kind || "素材"} />
                    <p className="muted">
                      {`${asset.character_id || "通用"} · ${asset.license_status || "许可待确认"}`}
                    </p>
                    {source && (
                      <p>
                        {/^https?:\/\//.test(source) ? (
                          <a href={source} target="_blank" rel="noopener noreferrer">
                            查看来源
                          </a>
                        ) : (
                          display(source)
                        )}
                      </p>
                    )}
                  </div>
                </article>
              );
            })
          )}
        </div>
      </div>
    </section>
  );
}
