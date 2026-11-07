"use strict";
import { MemePicker, memePreviewUrl } from "./memes.js";
import { contactLabel, contactReason, contactStatus } from "./modules/message-labels.js?v=20261107.20";

export class MessagesPanel {
  constructor(root,api,notify,openProfile) {
    this.root=root;this.api=api;this.notify=notify;this.value=null;this.selected=null;this.user=null;this.dirty=false;this.policyDirty=false;this.busy=false;
    this.unsavedByAccount=new Map();this.edits=new Map();this.draftView='pending';this.refreshRequest=0;this.conversationRequest=0;this.memeCache=new Map();this.pickerContext=null;
    root.innerHTML=`<div class="page-heading"><div><p class="eyebrow">PRIVATE MESSAGES</p><h1>私信工作台</h1></div><div class="actions"><button data-sync>读取最新私信</button><button data-refresh>刷新</button><a href="#message-policy-settings">私信设置 →</a></div></div>
      <p class="hint">回复对方主动发来的私信；收到真实私聊邀请或完成约定后，可主动联系一次，无回应则等待。收件人绑定原消息作者，不能由模型改成其他用户。同一OC与平台用户共享好感度，评论与私信正文各自保留。</p>
      <p data-override class="feature-note" hidden><span data-override-text></span> <a href="#automation">自主浏览与互动 →</a></p>
      <details class="panel"><summary>私信策略与发送限额</summary><form data-policy><div class="checks"><label><input type="checkbox" name="enabled">启用私信</label><label><input type="checkbox" name="auto_poll">自动读取来信</label><label><input type="checkbox" name="auto_send">自动发送有来源触发的回复</label><label><input type="checkbox" name="allow_comment_handoff">收到私聊邀请或完成约定后联系</label></div><div class="fields"><label>读取间隔（秒）<input name="poll_interval_seconds" type="number" min="20" max="86400"></label><label>每小时最多发送<input name="max_per_hour" type="number" min="0" max="30" required></label><label>每天最多发送<input name="max_per_day" type="number" min="0" max="200" required></label><label>同用户冷却（秒）<input name="recipient_cooldown_seconds" type="number" min="10" max="86400"></label><label>首发联系核对输出额度（tokens，含思考）<input name="channel_max_tokens" type="number" min="256" max="65536"></label></div><button type="submit">保存私信策略</button><p class="hint">发送限额填 0 表示不限。启用私信、自动读取与自动发送后，将启用后新收到的连续来信合并回复，不追发历史来信，也无需逐条审核。真实私聊邀请或完成约定可触发首次联系一次，无回应则等待；普通来信回复不额外做首发联系核对。关闭自动发送时保留待审核草稿，手工生成仍先审核。首发联系核对默认2048 tokens，沿用模型思考设置；正常回复额度不变。B站账号凭证在设置页统一管理。允许表情包时，仅简单情绪回复可选择图片；知识问答与引用仍使用文字。</p></form></details>
      <p data-status role="status"></p><p data-error class="error-text" role="alert"></p>
      <div class="message-layout"><section class="panel"><h2>用户与来信</h2><label>会话<select data-users><option value="">选择用户</option></select></label><p data-relation class="hint"></p><div data-inbox></div></section><section class="panel"><h2>私信草稿与回执</h2><div data-drafts class="message-drafts"></div><p data-source class="quoted-text">选择草稿查看来源和回复。</p><p data-draft-status></p><label>私信内容<textarea data-text rows="6" disabled></textarea></label><div class="actions"><button data-review="edit">保存草稿</button><button data-review="approve" class="primary">审核通过</button><button data-review="reject">拒绝</button><button data-copy>复制已审核内容</button><button data-send class="primary">发送已审核私信</button><button data-retry>重试生成</button></div><pre data-receipt class="code-small"></pre></section></div>`;
    this.$=selector=>root.querySelector(selector);
    const views=this.element('fieldset',null,'queue-status-options');views.dataset.draftViews='';
    views.innerHTML='<legend>私信记录</legend><label><input type="radio" name="message-draft-view" value="pending" checked>待处理</label><label><input type="radio" name="message-draft-view" value="history">历史</label>';
    this.$('[data-drafts]').before(views);
    views.onchange=event=>{this.rememberDraft();this.draftView=event.target.value;this.selected=null;this.dirty=false;this.$('[data-error]').textContent='';this.render();};
    const profileButton = this.element("button", "用户档案");
    const conversationState = this.element('p', '', 'hint');conversationState.dataset.conversationState='';conversationState.setAttribute('role','status');
    this.$('[data-inbox]').before(conversationState);
    profileButton.type = "button";
    profileButton.dataset.userProfile = "";
    profileButton.hidden = true;
    this.$("[data-relation]").after(profileButton);
    profileButton.onclick = () => {
      const accountId = this.value?.account_id;
      const user = this.value?.users.find(item => `${item.platform}|${item.platform_user_id}` === this.user);
      if (accountId && user) openProfile({ platform: user.platform, userId: user.platform_user_id,
        accountId, name: user.display_name, onChange: () => this.refresh() });
    };
    const progress=this.element('section');progress.dataset.messageContinuation='';this.$('[data-receipt]').before(progress);this.continuation=new OperationContinuationView(progress,api,notify,()=>this.refresh());
    this.privateSelected=null;
    const privateSection=this.element('section',null,'panel');privateSection.dataset.privateGenerations='';privateSection.hidden=true;
    privateSection.append(this.element('h2','私域生成进度'),this.element('p','这里保留移交私聊及渠道尚待确认的生成原稿。','hint'));
    const privateList=this.element('div',null,'message-drafts');privateList.dataset.privateGenerationList='';
    const privateProgress=this.element('div');privateProgress.dataset.privateGenerationProgress='';privateSection.append(privateList,privateProgress);root.append(privateSection);
    this.privateContinuation=new OperationContinuationView(privateProgress,api,notify,()=>this.refresh());
    const memePolicy=document.createElement('label');const memeToggle=document.createElement('input');memeToggle.type='checkbox';memeToggle.name='use_memes';memePolicy.append(memeToggle,'允许模型用表情包替代简单情绪回复');this.$('[data-policy] .checks').append(memePolicy);
    const imageActions=this.element('div',null,'actions dm-meme-actions');
    const pick=this.element('button','选择表情包');pick.type='button';pick.dataset.pickMeme='';
    const clear=this.element('button','改用文字');clear.type='button';clear.dataset.clearMeme='';
    const manage=this.element('a','管理表情包图库 →');manage.href='#assets';imageActions.append(pick,clear,manage);
    const imagePreview=this.element('div',null,'dm-meme-preview');imagePreview.dataset.memePreview='';imagePreview.hidden=true;
    imagePreview.innerHTML='<strong data-meme-name></strong><figure><img data-meme-image alt="已选表情包" hidden><span data-meme-fallback class="hint" hidden>图片预览不可用，请检查图库。</span></figure><p data-meme-availability class="hint"></p><p class="hint">发送时只有这一张图片；下方文字仅供审阅或切回文字，不会一并发送。</p>';
    const textLabel=this.$('[data-text]').parentElement;const title=this.element('span','私信内容');title.dataset.textLabel='';textLabel.firstChild.replaceWith(title);textLabel.before(imageActions,imagePreview);
    this.memePicker=new MemePicker(api,(id,asset)=>this.chooseMeme(id,asset,this.pickerContext));
    pick.onclick=()=>{const row=this.value?.drafts.find(item=>item.id===this.selected);if(!row)return;this.pickerContext={id:row.id,account:this.value.account_id,character:row.character_id};this.memePicker.open(row.character_id,row.meme_id);};
    clear.onclick=()=>this.chooseMeme(null).catch(error=>{this.$('[data-error]').textContent=error.message;});
    this.$('[data-refresh]').onclick=()=>this.refresh();
    this.$('[data-sync]').onclick=()=>this.action(async()=>{const result=await api('/api/messages/sync',{method:'POST'});notify(`读取完成，新增 ${result.imported} 条私信${result.errors.length?'，部分会话读取失败':''}`);await this.refresh();});
    this.$('[data-sync]').disabled=true;this.$('[data-policy] button').disabled=true;
    this.$('[data-policy]').oninput=()=>{this.policyDirty=true;this.$('[data-status]').textContent='策略有未保存修改。';};
    this.$('[data-policy]').onsubmit=event=>{event.preventDefault();this.action(async()=>{const value={};for(const input of this.$('[data-policy]').querySelectorAll('input'))value[input.name]=input.type==='checkbox'?input.checked:Number(input.value);await this.savePolicy(value);});};
    this.$('[data-users]').onchange=()=>{this.rememberDraft();this.user=this.$('[data-users]').value;this.selected=null;this.dirty=false;this.inboxSignature=null;++this.conversationRequest;this.$('[data-inbox]').replaceChildren();this.$('[data-conversation-state]').textContent=this.user?'正在读取会话…':'';this.$('[data-relation]').textContent='';this.$('[data-error]').textContent='';this.render();this.conversation();};
    this.$('[data-text]').oninput=()=>{const row=this.value?.drafts.find(item=>item.id===this.selected);this.dirty=this.$('[data-text]').value!==(row?.text||'');if(this.dirty)this.rememberDraft();else this.edits.delete(this.editKey());this.renderEditor();};
    for(const button of root.querySelectorAll('[data-review]'))button.onclick=()=>this.action(async()=>{const id=this.selected;const result=await api(`/api/messages/drafts/${id}/review`,{method:'POST',body:{action:button.dataset.review,text:this.$('[data-text]').value}});this.applyDraftResult(result,id);await this.refresh();});
    this.$('[data-copy]').onclick=()=>this.action(async()=>{const id=this.selected;await navigator.clipboard.writeText(this.$('[data-text]').value);const result=await api(`/api/messages/drafts/${id}/review`,{method:'POST',body:{action:'copy'}});this.applyDraftResult(result,id);await this.refresh();});
    this.$('[data-send]').onclick=()=>this.action(async()=>{const row=this.value?.drafts.find(item=>item.id===this.selected);if(!row)return;try{if(row.message_kind==='image')await api(`/api/messages/drafts/${row.id}/review`,{method:'POST',body:{action:'approve'}});const result=await api(`/api/messages/drafts/${row.id}/send`,{method:'POST'});this.applyDraftResult(result,row.id);if(result.status==='published')notify(row.message_kind==='image'?'单张图片已发送，平台回执已保存。':'私信已发送，平台回执已保存。');}finally{await this.refresh();}});
    this.$('[data-retry]').onclick=()=>this.action(async()=>{await api(`/api/messages/drafts/${this.selected}/retry`,{method:'POST'});await this.refresh();});
    this.renderEditor();
  }
  mountSettings(container){
    if(!container)return;
    this.policyRoot=this.$('[data-policy]').closest('details');
    this.policyRoot.open=true;
    container.append(this.policyRoot);
    this.$=selector=>this.root.querySelector(selector)||this.policyRoot.querySelector(selector);
  }
  settings(){const value={},base=this.value.policy.base||this.value.policy;for(const name of ['enabled','auto_poll','auto_send','allow_comment_handoff','use_memes','poll_interval_seconds','max_per_hour','max_per_day','recipient_cooldown_seconds','channel_max_tokens'])value[name]=base[name];return value;}
  closeImagePicker(){this.pickerContext=null;this.memePicker.close(true);}
  async chooseMeme(meme_id,asset=null,context=null){
    if(this.busy)throw new Error('草稿正在处理中，请稍后再选图。');
    const row=this.value?.drafts.find(item=>item.id===this.selected);if(!row)throw new Error('请先选择草稿。');
    if(context&&(context.id!==row.id||context.account!==this.value.account_id||context.character!==row.character_id))throw new Error('账号或草稿已变化，请重新选图。');
    const account=this.value.account_id,id=row.id,text=this.$('[data-text]').value;
    this.busy=true;this.renderEditor();this.$('[data-error]').textContent='';
    try{await this.api(`/api/messages/drafts/${id}/review`,{method:'POST',body:{action:'edit',meme_id,...(text.trim()?{text}:{})}});if(this.value.account_id!==account||this.selected!==id)return;if(asset)this.memeCache.set(`${row.character_id}:${asset.asset_id}`,asset);row.message_kind=meme_id?'image':'text';row.meme_id=meme_id;row.meme=asset;row.status='pending_review';this.dirty=false;await this.refresh();}
    catch(error){this.$('[data-error]').textContent=error.message;throw error;}
    finally{this.busy=false;this.renderEditor();}
  }
  async savePolicy(value){await this.api('/api/messages/policy',{method:'PUT',body:value});this.policyDirty=false;await this.refresh();}
  async action(work){if(this.busy)return;this.busy=true;this.renderEditor();this.$('[data-error]').textContent='';try{await work();}catch(error){this.$('[data-error]').textContent=error.message;}finally{this.busy=false;this.renderEditor();}}
  element(tag,text,cls){const node=document.createElement(tag);if(text!=null)node.textContent=text;if(cls)node.className=cls;return node;}
  editKey(id=this.selected){return `${this.value?.account_id}/${id}`;}
  rememberDraft(){if(this.dirty&&this.selected!=null)this.edits.set(this.editKey(),this.$('[data-text]').value);}
  selectDraft(id){this.rememberDraft();this.selected=id;const key=this.editKey();this.dirty=this.edits.has(key);if(this.dirty)this.$('[data-text]').value=this.edits.get(key);this.$('[data-error]').textContent='';this.render();}
  applyDraftResult(result,id){if(!result?.status)return;this.edits.delete(this.editKey(id));if(this.selected!==id)return;this.dirty=false;if(this.draftState(result).history){this.selected=null;this.closeImagePicker();}}
  hasUnsaved(){return this.dirty||this.policyDirty||this.unsavedByAccount.size>0||this.edits.size>0;}
  async refresh({background=false}={}){
    if(this.root.closest('[hidden]'))return;
    if(background&&(this.busy||this.backgroundPending||Date.now()-(this.refreshedAt||0)<8000))return;
    if(background)this.backgroundPending=true;
    this.refreshedAt=Date.now();
    const request=++this.refreshRequest;
    try{
      const value=await this.api('/api/messages');
      if(request!==this.refreshRequest)return;
      if(this.value&&this.value.account_id!==value.account_id){
        this.privateSelected=null;this.privateContinuation.render(null);this.$('[data-private-generation-list]').replaceChildren();
        this.closeImagePicker();
        if(this.dirty&&this.selected)this.unsavedByAccount.set(this.value.account_id,{selected:this.selected,user:this.user,text:this.$('[data-text]').value});
        this.selected=null;this.user=null;this.dirty=false;this.$('[data-text]').value='';this.inboxSignature=null;
        const saved=this.unsavedByAccount.get(value.account_id);
        if(saved&&value.drafts.some(row=>row.id===saved.selected)){
          this.selected=saved.selected;this.user=saved.user;this.dirty=true;this.$('[data-text]').value=saved.text;this.unsavedByAccount.delete(value.account_id);
        }
        this.$('[data-error]').textContent='账号范围已变化，已隐藏旧会话。未保存编辑只留在当前浏览器对应账号内，不会写入其他账号。';
      }
      const selected=value.drafts.find(row=>row.id===this.selected);
      if(this.selected!=null&&!this.dirty&&(!selected||(this.draftView!=='history'&&this.draftState(selected).history))){this.selected=null;this.closeImagePicker();}
      this.value=value;this.render();await this.conversation();
    }catch(error){if(request===this.refreshRequest)this.$('[data-error]').textContent=error.message;}
    finally{if(background)this.backgroundPending=false;}
  }
  policyStatus(p){const base=p.base||p,saved=`已配置${base.auto_poll?'自动读取':'手动读取'} · ${base.auto_send?'自动回复':'审核后发送'}`;if(p.emergency)return `紧急停止；${saved}`;if(p.automation_override===false)return `自主运营已停止；${saved}`;return `${p.auto_poll?'自动读取':'手动读取'} · ${p.auto_send?'自动回复':'审核后发送'}`;}
  render(){const p=this.value.policy,base=p.base||p;this.$('[data-sync]').disabled=!p.enabled;this.$('[data-policy] button').disabled=false;this.$('[data-status]').textContent=`${this.value.account_id?'当前账号 '+this.value.account_id:'当前账号尚未核验'} · ${p.enabled?'私信已启用':'私信已关闭'} · ${this.policyStatus(p)}${!p.credentials_configured?' · 尚未配置账号凭证':''}${this.policyDirty?' · 基础策略有未保存修改':''}`;if(p.last_error)this.$('[data-error]').textContent=p.last_error;
    this.$('[data-override]').hidden=!p.automation_override&&!p.emergency;this.$('[data-override-text]').textContent=p.emergency?'紧急停止已生效，所有自动收发被阻止。这里编辑个人基础设置；解除紧停前不会恢复自动业务。':'自主运营正在运行，按已保存的私信权限处理。设置保存后生效；统一停止请回到活动页。';
    if(!this.policyDirty)for(const input of this.$('[data-policy]').querySelectorAll('input')){if(input.type==='checkbox')input.checked=base[input.name];else input.value=base[input.name];}
    const select=this.$('[data-users]');const userSignature=JSON.stringify(this.value.users.map(user=>[user.platform,user.platform_user_id,user.display_name]));if(userSignature!==this.userSignature){this.userSignature=userSignature;select.replaceChildren(this.element('option','选择用户'));select.firstChild.value='';for(const user of this.value.users){const option=this.element('option',`${user.display_name||'用户'} · ${user.platform_user_id}`);option.value=`${user.platform}|${user.platform_user_id}`;select.append(option);}}select.value=this.user||'';
    const drafts=this.value.drafts.filter(row=>(!this.user||`${row.platform}|${row.platform_user_id}`===this.user)&&(this.draftState(row).history===(this.draftView==='history')));
    for(const radio of this.$('[data-draft-views]').querySelectorAll('input')){radio.checked=radio.value===this.draftView;radio.disabled=this.busy;}
    const draftSignature=JSON.stringify([this.user,this.draftView,drafts,this.selected]);if(draftSignature!==this.draftSignature){this.draftSignature=draftSignature;this.$('[data-drafts]').replaceChildren(...drafts.map(row=>{const button=this.element('button',`#${row.id} ${this.contactLabel(row)} · ${row.display_name||row.platform_user_id} · ${this.draftState(row).label}`);button.setAttribute('aria-pressed',String(row.id===this.selected));button.onclick=()=>this.selectDraft(row.id);return button;}));
    if(!drafts.length)this.$('[data-drafts]').append(this.element('p',this.draftView==='history'?'暂无历史私信记录。':'暂无待处理私信。新来信按当前自动读取与发送设置处理。','hint'));}this.renderEditor();this.renderPrivateContinuations();
  }
  renderPrivateContinuations(){
    const rows=this.value?.private_continuations||[],section=this.$('[data-private-generations]'),list=this.$('[data-private-generation-list]');
    section.hidden=!rows.length;
    if(!rows.some(row=>row.id===this.privateSelected))this.privateSelected=rows[0]?.id||null;
    list.replaceChildren(...rows.map(row=>{const button=this.element('button',`${row.source_title||'私域生成记录'} #${String(row.id).slice(0,8)} · ${this.privateContinuation.label(row.state)}`);button.type='button';button.setAttribute('aria-pressed',String(row.id===this.privateSelected));button.onclick=()=>{this.privateSelected=row.id;this.renderPrivateContinuations();};return button;}));
    const row=rows.find(row=>row.id===this.privateSelected);
    this.privateContinuation.render(row,{channel:'messages',localId:null,privateContext:true});
  }
  label(status){return ({queued:'等待生成',generating:'生成中',pending_review:'待审核',approved:'已审核',rejected:'已拒绝',copied:'已复制',published:'已发送',publishing:'发送中',publish_uncertain:'发送结果待核对',failed:'生成失败',cancelled:'已取消',waiting:'等待约定或对方回应',not_initiated:'未发起私信'})[status]||status;}
  draftState(row){
    const status=row?.status,history=['published','rejected','copied','not_initiated'].includes(status);
    const label=contactStatus(row)||this.label(status);
    return {history,progress:['queued','generating'].includes(status),waiting:status==='waiting',continueAgreement:status==='waiting'&&row?.contact_state==='awaiting_completion',label};
  }
  contactLabel(row){return contactLabel(row);}
  renderTrigger(row){
    let box=this.$('[data-trigger]');
    if(!box){box=this.element('details');box.dataset.trigger='';const title=this.element('summary','触发来源与联系目的');box.append(title,this.element('div'));this.$('[data-source]').after(box);}
    box.hidden=!row;
    const body=box.querySelector('div');body.replaceChildren();if(!row)return;
    body.append(this.element('p',`${this.contactLabel(row)} · ${row.purpose||'未记录联系目的'}`));
    let source=row.trigger_source;
    if(typeof source==='string'){try{source=JSON.parse(source);}catch{source=null;}}
    if(source?.kind==='comment'){
      const link=this.element('a',`公开评论 ${source.bvid||''} · 楼 ${source.root||'未知'} · 评论 ${source.comment_id||'未知'}`);
      if(/^BV[0-9A-Za-z]{10}$/.test(source.bvid||'')&&/^[1-9][0-9]*$/.test(String(source.comment_id||''))){link.href=`https://www.bilibili.com/video/${source.bvid}#reply${source.comment_id}`;link.target='_blank';link.rel='noopener noreferrer';}
      body.append(link);
    }else if(source?.kind==='message'){body.append(this.element('p',`对方来信 · 本地编号 ${(source.ids||[row.source_id]).join('、')} · 平台消息 ${source.message_id||'未记录'}`));}
    else body.append(this.element('p','旧记录未保存结构化触发来源，以上方原来源为准。','hint'));
    const check=row.outreach_check,assessment=check?.assessment;
    if(assessment?.reason)body.append(this.element('p',`联系判断：${assessment.reason}`));
    if(assessment?.invitation_quote)body.append(this.element('p',`邀请或约定原话：${assessment.invitation_quote}`));
    if(check?.previous_draft_id)body.append(this.element('p',`关联先前私信 #${check.previous_draft_id}`));
  }
  renderEditor(){const row=this.value?.drafts.find(row=>row.id===this.selected),state=this.draftState(row);this.continuation.render(row?.continuation,{channel:'messages',localId:row?.id,dirty:this.dirty,entityStatus:row?.status});const terminalInPending=this.draftView!=='history'&&state.history;const editable=row&&!terminalInPending&&['pending_review','approved','rejected','copied'].includes(row.status);const text=this.$('[data-text]');if(!this.dirty&&text.value!==(row?.text||''))text.value=row?.text||'';text.disabled=this.busy||(!editable&&!this.dirty);text.readOnly=!editable;text.hidden=!this.dirty&&(state.progress||state.waiting);this.$('[data-text-label]').hidden=text.hidden;
    this.$('[data-users]').disabled=this.busy;
    for(const radio of this.$('[data-draft-views]').querySelectorAll('input'))radio.disabled=this.busy;
    const image=row?.message_kind==='image',asset=row?.meme||this.memeCache.get(`${row?.character_id}:${row?.meme_id}`),unavailable=image&&asset&&(!asset.enabled||asset.missing);
    this.$('[data-text-label]').textContent=image?'审阅文字 / 文字备选（不会随图片发送）':'私信内容';
    this.$('[data-pick-meme]').disabled=!editable||this.busy;this.$('[data-clear-meme]').disabled=!editable||this.busy||!image;this.$('[data-pick-meme]').hidden=!editable;this.$('[data-clear-meme]').hidden=!editable;
    this.$('[data-meme-preview]').hidden=!image;
    if(image){this.$('[data-meme-name]').textContent=asset?.title||row.meme_id||'已选图片';this.$('[data-meme-availability]').textContent=unavailable?'当前图片已禁用或缺失，请更换图片或改用文字。':'图片将在发送前重新检查可用性。';const key=`${row.character_id}:${row.meme_id}`;if(this.previewKey!==key){this.previewKey=key;const preview=this.$('[data-meme-image]');preview.hidden=true;this.$('[data-meme-fallback]').hidden=true;preview.onload=()=>{preview.hidden=false;this.$('[data-meme-fallback]').hidden=true;};preview.onerror=()=>{preview.hidden=true;this.$('[data-meme-fallback]').hidden=false;};preview.src=memePreviewUrl({asset_id:row.meme_id},row.character_id);}}
    else if(this.previewKey){this.previewKey=null;this.$('[data-meme-image]').removeAttribute('src');}
    this.$('[data-source]').textContent=row?`${this.contactLabel(row)} · ${row.display_name||row.platform_user_id} · UID ${row.platform_user_id} · ${row.source_kind==='comment'?'评论转入':row.source_kind==='message'?'对方来信':'来源记录'} #${row.source_id} · ${contactReason(row.reason)} · ${row.character_id} v${row.character_version}`:'选择草稿查看来源和回复。';this.renderTrigger(row);
    this.$('[data-draft-status]').textContent=this.dirty&&!editable?'原记录已处理或不在当前列表；本地修改已保留，可复制文字。':row?`${state.label}${row.purpose&&state.waiting?' · '+row.purpose:''}${row.error?' · '+row.error:''}${this.dirty?' · 有未保存修改':''}`:'';this.$('[data-receipt]').textContent=row?.receipt||'';
    for(const button of this.root.querySelectorAll('[data-review]')){button.disabled=!editable||this.busy;button.hidden=!editable;}
    this.$('[data-copy]').disabled=image||!row||!['approved','copied'].includes(row.status)||this.dirty||this.busy;
    this.$('[data-send]').textContent=image?'批准并发送图片':'发送已审核私信';
    this.$('[data-send]').disabled=!row||!(image?['pending_review','approved','copied']:['approved','copied']).includes(row.status)||this.dirty||this.busy||!!unavailable||!this.value.policy.enabled||!this.value.policy.credentials_configured;
    this.$('[data-send]').hidden=!editable;this.$('[data-copy]').hidden=!editable;
    for(const button of this.root.querySelectorAll('[data-drafts] button'))button.disabled=this.busy;
    const canRetry=state.continueAgreement||['failed','cancelled'].includes(row?.status);
    this.$('[data-retry]').textContent=state.continueAgreement?'继续完成约定':'重试生成';
    this.$('[data-retry]').disabled=!canRetry||this.busy;
    this.$('[data-retry]').hidden=!canRetry||(!state.continueAgreement&&row?.continuation?.phase==='generation');
  }
  async conversation(){const request=++this.conversationRequest;const profile=this.$('[data-user-profile]');profile.hidden=!this.user||!this.value?.account_id;if(!this.user){this.$('[data-conversation-state]').textContent='';this.$('[data-inbox]').replaceChildren();this.$('[data-relation]').textContent='';return;}const account=this.value.account_id;const [platform,user_id]=this.user.split('|');let value;try{value=await this.api(`/api/messages/conversation?platform=${encodeURIComponent(platform)}&user_id=${encodeURIComponent(user_id)}`);}catch(error){if(request===this.conversationRequest){this.$('[data-error]').textContent=error.message;this.$('[data-conversation-state]').textContent='会话读取失败，可点击刷新重试。';}return;}if(request!==this.conversationRequest||this.user!==`${platform}|${user_id}`||this.value.account_id!==account||('account_id' in value&&value.account_id!==account))return;
    const r=value.relationship;this.$('[data-relation]').textContent=typeof r.affinity==='number'?`与当前OC的好感度 ${r.affinity} / 100 · ${r.stage}（评论／私信共享）`:'个性化未启用';this.$('[data-conversation-state]').textContent=value.messages.length?'':'此会话暂无来信。';
    const signature=JSON.stringify([this.user,value.messages,value.drafts.map(draft=>[draft.id,draft.source_kind,draft.source_id])]);if(signature===this.inboxSignature)return;this.inboxSignature=signature;
    this.$('[data-inbox]').replaceChildren(...value.messages.map(row=>{const item=this.element('article',null,'message-incoming');item.dataset.messageId=String(row.id);item.dataset.platformUserId=String(row.platform_user_id||user_id);item.append(this.element('small',new Date(row.sent_at*1000).toLocaleString()),this.element('p',row.content));const known=value.drafts.find(draft=>draft.source_kind==='message'&&draft.source_id===row.id);const button=this.element('button',known?'查看已有回复':'生成私信回复');button.onclick=()=>this.action(async()=>{const result=known?{draft_id:known.id}:await this.api(`/api/messages/inbox/${row.id}/reply`,{method:'POST'});this.draftView=known&&this.draftState(known).history?'history':'pending';await this.refresh();this.selectDraft(result.draft_id);});item.append(button);return item;}));
  }
}
