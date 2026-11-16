const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
class Element {
  constructor(tag = 'div', text = '') {this.tagName = tag.toUpperCase(); this.children = []; this.textContent = text; this.value = ''; this.hidden = false; this.disabled = false; this.handlers = {}; this.attrs = {}; this.classList = {toggle() {}, add() {}};}
  set innerHTML(_) {throw Error('Unsafe innerHTML assignment');}
  append(...children) {this.children.push(...children);}
  replaceChildren(...children) {this.children = children; if(this.tagName === 'SELECT') this.value = children[0]?.value || '';}
  add(child) {this.children.push(child);}
  get options() {return this.children;}
  setAttribute(key, value) {this.attrs[key] = value;}
  addEventListener(name, handler) {this.handlers[name] = handler;}
  focus() {}
  scrollIntoView() {}
}
const ids = ['views','metadata-filters','nav','nav-status','refresh','run-search','load-more','records','detail','heading','hint','count','warnings','collection','type','search','search-form','text-mode','semantic-mode','search-hint','management'];
const elements = Object.fromEntries(ids.map(id => [id, new Element(['collection','type'].includes(id) ? 'select' : 'div')]));
const catalog = new Element('section');
const tabs = ['library', 'contexts', 'history'].map(section => Object.assign(new Element('button'), {dataset:{section}}));
const calls = [];
let slowContexts = false;
const boolFields = {documents_approval_required:false, instructions_approval_required:true, git_history_enabled:true};
let delayed = false;
let failRecord = false;
const fixture = {key:['workspace','demo','one'],title:'<img src=x onerror=alert(1)>',description:'Live description',type:'knowledge',content:'# Actual content\n\n<script>alert(1)</script>\n\n- first\n- second\n\n```js\nconst x = 1;\n```',metadata:{status:'active'},links:[{key:['workspace','demo','two'],title:'Next record'},{reference:'missing',title:'Unavailable',unavailable:true}]};
const response = value => ({ok:true,json:async()=>value});
const fetch = async (path, options) => {
  calls.push(path);
  const url = new URL(path, 'http://localhost');
  if(url.pathname === '/api/contexts') {
    if (slowContexts) await new Promise(resolve=>setTimeout(resolve,60));
    return response({settings:boolFields,sources:{},contexts:[{id:'ctx',title:'Project',workdirs:['/work'],settings:boolFields,sources:{git_history_enabled:'context.configuration'}}]});
  }
  if(url.pathname === '/api/context-settings' || url.pathname === '/api/history/revert') { calls.push(JSON.parse(options.body)); return response({saved:true}); }
  if(url.pathname === '/api/history/diff') return response({diff:'+ change'});
  if(url.pathname === '/api/history') return response([{commit:'0123456789abcdef',timestamp:'today',subject:'Change',files:[{action:'M',path:'documents/notes/a.md'}]}]);
  if(url.pathname === '/api/modules') return response({modules:[{id:'workspace',title:'Workspace',available:true},{id:'broken',title:'Broken',available:false,error:'Not installed'}]});
  if(url.pathname === '/api/collections') return response({collections:[{id:'demo',title:'Actual collection'}],filters:[{name:'source',label:'Source / client'}]});
  if(url.pathname === '/api/record') {
    if(failRecord) {failRecord=false;return {ok:false,json:async()=>({error:'Temporary record error'})};}
    const offset=Number(url.searchParams.get('offset') || 0);
    return response({...fixture,key:['workspace','demo',url.searchParams.get('id')],metadata:{...fixture.metadata,content_source:'existing catalog conversation blocks',pagination:{offset,limit:2,returned:2,total:6,has_more:offset<4,next_offset:offset+2}}});
  }
  const query = url.searchParams.get('query');
  if(delayed && query === 'old') await new Promise(resolve=>setTimeout(resolve,80));
  return response({records:[{...fixture,title:query || fixture.title}],total:1,has_more:false,types:['knowledge'],diagnostics:{algorithm:'balanced',counts:{considered_documents:12,selected:1},completeness:{result_window_complete:false},boundary:{result_cap:{omitted:3}}}});
};
const context = {document:{getElementById:id=>elements[id],createElement:tag=>new Element(tag),createTextNode:text=>({textContent:text}),querySelectorAll:()=>tabs,querySelector:()=>catalog},Option:function(text,value){return Object.assign(new Element('option',text),{value});},fetch,AbortController,URLSearchParams,console,setTimeout,clearTimeout,window:{matchMedia:()=>({matches:false})}};
vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),context);
const wait = ms=>new Promise(resolve=>setTimeout(resolve,ms));
const allText = el => [el.textContent,...(el.children || []).map(allText)].join(' ');
(async()=>{
  await wait(20);
  assert.equal(elements.heading.textContent,'Workspace');
  const sourceFilter=elements['metadata-filters'].children[0].children[1];
  sourceFilter.value='claude-code';
  sourceFilter.handlers.change();
  await wait(10);
  assert(calls.some(path=>JSON.parse(new URL(path,'http://localhost').searchParams.get('filters') || '{}').source==='claude-code'));
  sourceFilter.value='';

  assert.equal(elements.records.children.length,1);
  assert.match(allText(elements.detail),/Actual content/);
  assert.match(allText(elements.detail),/<script>alert\(1\)<\/script>/);
  assert.match(allText(elements.detail),/Unavailable/);
  let paging=elements.detail.children.find(child=>child.className==='content-paging');
  assert.match(allText(paging),/Conversation blocks 1–2 of 6 · partial content/);
  assert.match(allText(paging),/existing catalog conversation blocks/);
  failRecord=true;
  paging.children.at(-1).children[1].handlers.click();
  await wait(10);
  assert.match(allText(elements.detail),/Temporary record error/);
  elements.detail.children.at(-1).handlers.click();
  await wait(10);
  assert(calls.at(-1).includes('offset=2&limit=2'));
  paging=elements.detail.children.find(child=>child.className==='content-paging');
  assert.match(allText(paging),/Conversation blocks 3–4 of 6 · partial content/);
  paging.children.at(-1).children[0].handlers.click();
  await wait(10);
  assert(calls.at(-1).includes('offset=0&limit=2'));
  elements['semantic-mode'].handlers.click();
  await wait(10);
  elements.search.value='needle';
  elements['search-form'].handlers.submit({preventDefault(){}});
  await wait(10);
  assert(calls.some(path=>path.includes('mode=semantic') && path.includes('query=needle')));
  assert.match(elements.count.textContent,/balanced.*12 considered.*1 selected/);
  assert.match(elements.warnings.textContent,/3 records omitted/);
  assert(elements.detail.children.some(child=>child.tagName === 'DETAILS'));
  delayed=true;
  elements.search.value='old';
  elements['search-form'].handlers.submit({preventDefault(){}});
  elements.search.value='new';
  elements['search-form'].handlers.submit({preventDefault(){}});
  await wait(120);
  assert.match(allText(elements.records),/new/);
  assert.doesNotMatch(allText(elements.records),/old/);
  elements.nav.children[1].handlers.click();
  await wait(10);
  assert.match(allText(elements.records),/Not installed/);
  assert.equal(elements['run-search'].disabled,true);
  elements.refresh.handlers.click();
  await wait(20);
  assert.equal(elements['run-search'].disabled,true);
  tabs[1].handlers.click();
  await wait(10);
  assert.match(allText(elements.management), /Project.*\/work/);
  const table = elements.management.children[1];
  assert.equal(table.tagName, 'TABLE');
  const toggle = table.children[2].children[4].children[0].children[0];
  toggle.checked = false;
  await toggle.handlers.change();
  assert(calls.some(call => typeof call === 'object' && call.context === 'ctx' && call.field === 'git_history_enabled' && call.value === false));
  tabs[2].handlers.click();
  await wait(10);
  const card = elements.management.children[1];
  const row = card.children[2];
  await row.children[1].handlers.click();
  assert.match(allText(card), /\+ change/);
  await row.children[2].handlers.click();
  assert(calls.some(call => typeof call === 'object' && call.commit === '0123456789abcdef'));
  slowContexts = true;
  tabs[1].handlers.click();
  tabs[2].handlers.click();
  await wait(100);
  assert.match(allText(elements.management), /Edit history/);
  assert.doesNotMatch(allText(elements.management), /Project/);
  console.log('PASS: module-defined metadata filters, live load, full content, safe text, semantic submit, stale request isolation, unavailable modules, refresh, semantic diagnostics, folded metadata, content pagination and paging retry.');
})().catch(error=>{console.error(error);process.exitCode=1;});
