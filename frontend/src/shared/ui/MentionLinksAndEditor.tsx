import { useEffect, useState } from "react";

export interface PublicMention {
  uid: string | number;
  name: string;
}

export interface PendingMentionCandidate {
  uid: string | number;
  name: string;
}

export interface PendingMention {
  label: string;
  account?: string;
  candidates?: PendingMentionCandidate[];
}

export interface MentionSaveItem {
  label: string;
  account: string;
}

interface EditableRow {
  key: number;
  label: string;
  account: string;
  candidates: PendingMentionCandidate[];
}

const VALID_UID = /^[1-9][0-9]{0,19}$/;

export function MentionLinks({ mentions }: { mentions?: PublicMention[] | null }) {
  const valid = (mentions || []).filter((item) => VALID_UID.test(String(item.uid)));
  if (!valid.length) return null;

  return (
    <div className="actions">
      {valid.map((mention) => (
        <a
          key={`${mention.uid}-${mention.name}`}
          href={`https://space.bilibili.com/${mention.uid}`}
          target="_blank"
          rel="noopener noreferrer"
          title={`B 站 UID ${mention.uid}`}
        >
          @{mention.name}
        </a>
      ))}
    </div>
  );
}

export interface MentionLinksAndEditorProps {
  mentions?: PublicMention[] | null;
  pending?: PendingMention[] | null;
  editable?: boolean;
  onSave?: (items: MentionSaveItem[]) => Promise<void> | void;
}

function buildRows(
  mentions?: PublicMention[] | null,
  pending?: PendingMention[] | null
): EditableRow[] {
  const rows: EditableRow[] = [];
  let key = 1;
  for (const item of mentions || []) {
    rows.push({
      key: key++,
      label: `@${item.name}`,
      account: String(item.uid),
      candidates: [],
    });
  }
  for (const item of pending || []) {
    rows.push({
      key: key++,
      label: item.label || "",
      account: item.account || "",
      candidates: item.candidates || [],
    });
  }
  return rows;
}

export function MentionLinksAndEditor({
  mentions,
  pending,
  editable = false,
  onSave,
}: MentionLinksAndEditorProps) {
  const [rows, setRows] = useState<EditableRow[]>(() => buildRows(mentions, pending));
  const [nextKey, setNextKey] = useState(() => rows.length + 1);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const initial = buildRows(mentions, pending);
    setRows(initial);
    setNextKey(initial.length + 1);
  }, [mentions, pending]);

  if (!mentions?.length && !pending?.length && !editable) {
    return null;
  }

  return (
    <section>
      <MentionLinks mentions={mentions} />
      {Boolean(pending?.length) && (
        <p className="hint">待选账号 {pending!.map((item) => item.label).join("、")}</p>
      )}
      {editable && (
        <details>
          <summary>编辑 @</summary>
          <div>
            {rows.map((row) => (
              <div key={row.key} className="actions">
                <input
                  aria-label="正文中的 @昵称"
                  placeholder="正文中的 @昵称"
                  value={row.label}
                  onChange={(event) => {
                    const value = event.target.value;
                    setRows((prev) =>
                      prev.map((item) => (item.key === row.key ? { ...item, label: value } : item))
                    );
                  }}
                />
                <input
                  aria-label="B 站 UID 或昵称"
                  placeholder="B 站 UID 或昵称"
                  value={row.account}
                  onChange={(event) => {
                    const value = event.target.value;
                    setRows((prev) =>
                      prev.map((item) => (item.key === row.key ? { ...item, account: value } : item))
                    );
                  }}
                />
                <button
                  type="button"
                  onClick={() => setRows((prev) => prev.filter((item) => item.key !== row.key))}
                >
                  移除
                </button>
                {row.candidates.map((candidate) => (
                  <button
                    key={`${candidate.uid}-${candidate.name}`}
                    type="button"
                    onClick={() => {
                      setRows((prev) =>
                        prev.map((item) =>
                          item.key === row.key
                            ? { ...item, label: `@${candidate.name}`, account: String(candidate.uid) }
                            : item
                        )
                      );
                    }}
                  >
                    {candidate.name} · UID {candidate.uid}
                  </button>
                ))}
              </div>
            ))}
          </div>
          <div className="actions">
            <button
              type="button"
              onClick={() => {
                setRows((prev) => [...prev, { key: nextKey, label: "", account: "", candidates: [] }]);
                setNextKey((k) => k + 1);
              }}
            >
              添加 @
            </button>
            <button
              type="button"
              disabled={saving}
              onClick={async () => {
                if (!onSave) return;
                const cleaned = rows
                  .map((item) => ({ label: item.label.trim(), account: item.account.trim() }))
                  .filter((item) => item.label || item.account);
                setSaving(true);
                try {
                  await onSave(cleaned);
                } finally {
                  setSaving(false);
                }
              }}
            >
              保存提及
            </button>
          </div>
        </details>
      )}
    </section>
  );
}
