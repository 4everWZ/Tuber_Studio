import { useState } from "react";
import { useAppSession } from "@/shared/hooks/useAppSession";
import { emitNotice, errorMessage } from "@/shared/lib/errors";
import { BilibiliAccountPanel } from "./BilibiliAccountPanel";

export function AccountsPage() {
  const { localAccess, logout } = useAppSession();
  const [loggingOut, setLoggingOut] = useState(false);

  return (
    <section data-section="accounts" className="win-page-view">
      <div className="page-heading win-heading">
        <h1>Bilibili 账号</h1>
        <button
          id="logout"
          type="button"
          className="win-btn danger"
          hidden={localAccess}
          disabled={loggingOut}
          onClick={() => {
            setLoggingOut(true);
            void logout()
              .catch((error) => emitNotice(errorMessage(error), true))
              .finally(() => setLoggingOut(false));
          }}
        >
          退出登录工作台
        </button>
      </div>

      <BilibiliAccountPanel showSettings />

      <p id="comment-base-settings" data-workspace-anchor="" className="hint" style={{ marginTop: 14 }}>
        视频来源、监控频率和回复数量统一在<a href="#automation-settings">运营设置</a>中配置。
      </p>
    </section>
  );
}
