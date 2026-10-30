"use strict";

// Reject the upstream selector's first-result fallback. Missing source duration is
// tolerated only here; the downloaded stream must still pass the duration check.
const normalize = text => String(text || "").normalize("NFKC").toLowerCase().replace(/[\s\p{P}\p{S}]/gu, "");
const versions = song => [...new Set((`${song.name || ""} ${song.album?.name || ""}`.toLowerCase().match(/\blive\b|现场|演唱会|\bremix\b|混音|伴奏|纯音乐|\binstrumental\b|\bacoustic\b|不插电|\bdemo\b|\bremaster(?:ed)?\b|重制/g) || []))].sort().join("|");
function sameRecording(candidate, expected) {
  if (normalize(candidate.name) !== normalize(expected.name)) return false;
  if (versions(candidate) !== versions(expected)) return false;
  const names = new Set((candidate.artists || []).map(a => normalize(a.name)).filter(Boolean));
  const artists = (expected.artists || []).map(a => normalize(a.name)).filter(Boolean);
  if (!artists.length || names.size !== artists.length || artists.some(a => !names.has(a))) return false;
  return !candidate.duration || Math.abs(candidate.duration - expected.duration) <= 3000;
}
function select(list, info) {
  const candidate = list.find(song => sameRecording(song, info));
  if (!candidate) throw new Error("没有歌名、歌手、版本及时长一致的音源候选");
  return candidate;
}
select.ENABLE_FLAC = true;
module.exports = { normalize, sameRecording, select };
