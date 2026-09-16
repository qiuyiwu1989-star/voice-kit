// 最小假 DOM：只为让页面脚本的顶层能跑完，不渲染、不交互。
// 抓的是「定义前使用」「引用不存在的全局」这类加载即崩的错——2026-09-14 线上两次都是这类。
const mk = () => { const el = { style:{}, classList:{toggle(){},add(){},remove(){}}, hidden:false, value:'', textContent:'', innerHTML:'', className:'', disabled:false, parentElement:null, appendChild(){}, querySelector(){ return mk(); }, querySelectorAll(){ return []; }, remove(){}, scrollTop:0, scrollHeight:0, scrollIntoView(){}, dataset:{} }; el.parentElement = { hidden:false }; return el; };
globalThis.document = { querySelector: () => mk(), querySelectorAll: () => [], createElement: () => mk() };
globalThis.window = globalThis;
globalThis.localStorage = { getItem(){ return null; }, setItem(){}, get length(){ return 0; }, key(){ return null; } };
Object.defineProperty(globalThis, 'navigator', { value: { mediaDevices: { getUserMedia: async () => { throw new Error('no cam'); } } }, configurable: true });
globalThis.location = { pathname: '/talk/x', search: '' };
globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({ default: 1, options: [], sessions: [], facts: [], reviews: [] }), text: async () => '' });
globalThis.speechSynthesis = undefined; globalThis.MediaRecorder = undefined;
globalThis.URLSearchParams = URLSearchParams;
