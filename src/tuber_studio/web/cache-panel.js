"use strict";
class CachePanel {
  constructor(root, api, notify) {
    this.root=root; this.api=api; this.notify=notify; this.value=null; this.busy=false;
    root.innerHTML='<div class="panel-heading"><h2>媒体缓存</h2><button type="button" data-refresh>刷新容量</button></div><p class="hint">音乐最高15 GiB，合计导入歌曲、在线音频与演唱缓存。曲库引用、当前／待播和刚上传的素材受保护；移除曲库条目后可回收原文件。模型、音色包、账号和对话数据不在此清理。</p><form><div class="cache-groups"></div><button type="submit">保存缓存上限</button><p data-state role="status"></p></form>';
    this.groups=root.querySelector('.cache-groups');
    for (const [key,label,max] of [['music','音乐',15360],['speech','语音',15360],['temporary','临时产物',4096]]) {
      const group=document.createElement('div'); group.className='cache-group'; group.dataset.category=key;
      const name=document.createElement('h3');name.textContent=label;
      const stats=document.createElement('p');stats.dataset.stats='';stats.className='hint';
      const input=document.createElement('input');input.type='number';input.min=key==='music'?96:16;input.max=max;input.step=1;input.required=true;input.name=key+'_limit_mib';
      const field=document.createElement('label');field.textContent=`${label}最大缓存（MiB，最高${max}）`;field.append(input);
      const clean=document.createElement('button');clean.type='button';clean.textContent='清理可回收缓存';clean.setAttribute('aria-label',`清理${label}可回收缓存`);
      clean.onclick=()=>this.action(async()=>{const value=await api(`/api/cache/${key}/clear`,{method:'POST'});this.render(value);this.state(`已回收 ${this.bytes(value.result.freed_bytes)}，清理 ${value.result.removed_files} 个文件。`);});
      group.append(name,stats,field,clean);this.groups.append(group);
    }
    root.querySelector('[data-refresh]').onclick=()=>this.refresh();
    root.querySelector('form').onsubmit=event=>{event.preventDefault();this.action(async()=>{const settings={};for(const input of root.querySelectorAll('input'))settings[input.name]=Number(input.value);const value=await api('/api/cache',{method:'PUT',body:settings});this.render(value,true);this.state('容量设置已保存，超额可回收缓存已清理。');});};
  }
  bytes(value) { return value>=1073741824?`${(value/1073741824).toFixed(2)} GiB`:value>=1048576?`${(value/1048576).toFixed(1)} MiB`:value>=1024?`${(value/1024).toFixed(1)} KiB`:`${value} B`; }
  state(text,error=false){const state=this.root.querySelector('[data-state]');state.textContent=text;state.classList.toggle('error-text',error);}
  async action(work){if(this.busy)return;this.busy=true;this.root.querySelectorAll('button').forEach(button=>button.disabled=true);try{await work();}catch(error){this.state(error.message,true);}finally{this.busy=false;this.root.querySelectorAll('button').forEach(button=>button.disabled=false);}}
  async refresh(){if(this.busy||this.root.closest('[hidden]'))return;try{this.render(await this.api('/api/cache'));}catch(error){this.state(error.message,true);}}
  render(value,fields=false){const first=!this.value;this.value=value;for(const group of this.groups.children){const key=group.dataset.category,s=value.categories[key];group.querySelector('[data-stats]').textContent=`已用 ${this.bytes(s.bytes)} / ${this.bytes(s.limit_bytes)} · 可回收 ${this.bytes(s.reclaimable_bytes)} · 受保护 ${this.bytes(s.protected_bytes)}${s.writing?' · 正在写入':''}`;if(first||fields)group.querySelector('input').value=value.settings[key+'_limit_mib'];}}
}
