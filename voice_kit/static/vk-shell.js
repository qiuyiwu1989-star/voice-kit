/* voice-kit 统一后台 · 外壳（左侧导航 / 顶栏 / 主题 / 抽屉 / 搜索 / toast / 确认弹窗 / ⋯ 菜单）
 * 规范：docs/统一后台设计规范.md §4.1、§6.8、§6.12、§8。零依赖，零外部资源。
 * 在 <head> 里同步加载：第一件事是把主题写到 <html>，避免先闪一下别的颜色。
 * 页面脚本通过 window.VK 使用它；外壳在 DOMContentLoaded 时渲染进 #vkShell。 */
(function(){
  'use strict';
  var root = (typeof document !== 'undefined' && document.documentElement) || null;

  /* ------------------------------------------------------------ 主题
   * 默认暗色（2026-09-16 拍板），不跟随系统。手动切过就记住。
   * localStorage 在隐私模式 / 禁用站点数据时会直接抛错，所以读写都包 try。 */
  var THEME_KEY = 'vk-theme';
  function readTheme(){ try{ var t = localStorage.getItem(THEME_KEY); return t === 'light' ? 'light' : 'dark'; }catch(e){ return 'dark'; } }
  function applyTheme(t){ if(root && root.setAttribute) root.setAttribute('data-theme', t); }
  applyTheme(readTheme());

  var esc = function(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); };

  /* ------------------------------------------------------------ 图标（单色线性、24 网格、currentColor）*/
  var ICONS = {
    agent:'<rect x="4" y="5" width="16" height="13" rx="3"/><path d="M9 11h.01M15 11h.01M9.5 15c1.5 1 3.5 1 5 0M12 2v3"/>',
    memory:'<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6"/>',
    loop:'<path d="M4 12a8 8 0 0 1 13.7-5.6L20 9M20 4v5h-5M20 12a8 8 0 0 1-13.7 5.6L4 15M4 20v-5h5"/>',
    search:'<circle cx="11" cy="11" r="6.5"/><path d="m20 20-4.2-4.2"/>',
    chev:'<path d="m9 6 6 6-6 6"/>', down:'<path d="m6 9 6 6 6-6"/>', back:'<path d="m15 6-6 6 6 6"/>',
    lock:'<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
    shield:'<path d="M12 3 5 6v5c0 4.5 3 8.3 7 10 4-1.7 7-5.5 7-10V6z"/>',
    sun:'<circle cx="12" cy="12" r="4"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M5.6 18.4 7 17M17 7l1.4-1.4"/>',
    moon:'<path d="M19 14.5A7.5 7.5 0 0 1 9.5 5a7.5 7.5 0 1 0 9.5 9.5z"/>',
    plus:'<path d="M12 5v14M5 12h14"/>', x:'<path d="M6 6l12 12M18 6 6 18"/>', check:'<path d="m5 12.5 4.5 4.5L19 7.5"/>',
    info:'<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5M12 8h.01"/>',
    alert:'<path d="M12 4 3 20h18z"/><path d="M12 10v4M12 17h.01"/>',
    chat:'<path d="M5 5h14v10H10l-4 4v-4H5z"/>', export:'<path d="M12 4v11M8 11l4 4 4-4M5 19h14"/>',
    more:'<path d="M6 12h.01M12 12h.01M18 12h.01" stroke-width="2.6"/>', play:'<path d="M8 5v14l11-7z"/>',
    trash:'<path d="M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13"/>', menu:'<path d="M4 7h16M4 12h16M4 17h16"/>',
    empty:'<rect x="4" y="5" width="16" height="14" rx="2"/><path d="M4 13h4l2 2h4l2-2h4"/>',
    ext:'<path d="M14 5h5v5M19 5l-8 8M17 14v5H5V7h5"/>', undo:'<path d="M9 7 4 12l5 5M4 12h11a5 5 0 0 1 0 10h-2"/>'
  };
  function icon(name, cls){ return '<svg class="vk-ico' + (cls ? ' ' + cls : '') + '" viewBox="0 0 24 24" aria-hidden="true" focusable="false">' + (ICONS[name] || '') + '</svg>'; }

  var MODULES = [
    {id:'agents', href:'/admin/agents', label:'Agent 管理', icon:'agent'},
    {id:'memory', href:'/admin/memory', label:'用户记忆', icon:'memory'},
    {id:'feedback', href:'/admin/feedback', label:'数据回流', icon:'loop'}
  ];

  var $ = function(s, r){ return (r || document).querySelector(s); };
  var state = { module:'', searchFns:[], productFns:[], product:'', products:null, built:false };

  /* ------------------------------------------------------------ 产品筛选：URL ?product= 优先，其次 localStorage */
  var PRODUCT_KEY = 'vk-product';
  function storedProduct(){
    try{ var q = new URLSearchParams(location.search).get('product'); if(q) return q; }catch(e){}
    try{ return localStorage.getItem(PRODUCT_KEY) || ''; }catch(e){ return ''; }
  }
  function syncUrl(key, val){
    try{
      var u = new URL(location.href);
      if(val) u.searchParams.set(key, val); else u.searchParams.delete(key);
      history.replaceState(history.state, '', u.pathname + u.search + u.hash);
    }catch(e){}
  }
  function navHref(m){
    // 三页互跳时带上产品筛选
    return m.href + (state.product ? '?product=' + encodeURIComponent(state.product) : '');
  }

  /* ------------------------------------------------------------ 渲染外壳 */
  function build(){
    if(state.built || !document.body || !document.body.insertBefore) return;
    var host = $('#vkShell'); if(!host || !host.insertBefore) return;
    state.built = true;
    var body = document.body;
    state.module = (body.dataset && body.dataset.vkModule) || '';
    var env = ((body.dataset && body.dataset.vkEnv) || '线上').trim();
    if(!env || env.indexOf('{{') === 0) env = '线上';
    var isProd = env === '线上';
    var cur = MODULES.filter(function(m){ return m.id === state.module; })[0] || {label:''};
    state.product = storedProduct();

    var side = document.createElement('aside');
    side.className = 'vk-side'; side.id = 'vkSide';
    side.innerHTML =
      '<a class="vk-brand" href="/admin/agents" title="voice-kit 后台"><span class="vk-brand-mark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M4 9v6M9 5v14M14 8v8M19 10v4"/></svg></span><b>voice-kit</b><span class="kicker">Admin</span></a>' +
      '<nav class="vk-nav" aria-label="后台模块">' + MODULES.map(function(m){
        return '<a href="' + m.href + '" data-nav="' + m.id + '" title="' + esc(m.label) + '"' + (m.id === state.module ? ' aria-current="page"' : '') + '>' +
          icon(m.icon) + '<span class="lbl">' + esc(m.label) + '</span><span class="vk-count" data-count="' + m.id + '" hidden></span></a>';
      }).join('') + '</nav>' +
      '<div class="vk-side-foot"><p class="hint">系统只标问题，发布由人点。</p></div>';

    var top = document.createElement('header');
    top.className = 'vk-top';
    top.innerHTML =
      '<button class="vk-iconbtn vk-menu-btn" id="vkMenuBtn" aria-label="打开导航" aria-expanded="false" aria-controls="vkSide">' + icon('menu') + '</button>' +
      '<div class="vk-crumb" id="vkCrumb"><button class="vk-iconbtn back" id="vkBack" aria-label="返回列表" hidden>' + icon('back') + '</button><span>' + esc(cur.label) + '</span></div>' +
      '<label class="vk-select" id="vkProductWrap" hidden><span class="pre-lbl">产品</span><select id="vkProduct" aria-label="当前产品"></select>' + icon('down', 'chev') + '</label>' +
      '<div class="vk-search" id="vkSearchWrap" hidden><button class="vk-iconbtn vk-search-toggle" id="vkSearchToggle" aria-label="搜索（快捷键 /）">' + icon('search') + '</button>' + icon('search') +
        '<input id="vkSearch" type="search" autocomplete="off" aria-label="搜索当前列表" placeholder="搜索"><kbd aria-hidden="true">/</kbd></div>' +
      '<span class="vk-env' + (isProd ? '' : ' local') + '" title="' + (isProd ? '线上环境：这里的发布、删除都是真的' : '非线上环境：' + esc(env)) + '"><i aria-hidden="true"></i>' + esc(env) +
        (isProd ? '<span class="host">' + esc(location.host || '') + '</span>' : '') + '</span>' +
      '<span class="vk-ticket" id="vkTicket" title="页面能打开说明门票有效；接口返回 401 时这里会变红">' + icon('lock') + '<i aria-hidden="true"></i><span>门票有效</span></span>' +
      '<button class="vk-iconbtn" id="vkTheme" aria-label="切换主题"></button>';

    var skip = document.createElement('a');
    skip.className = 'vk-skip'; skip.href = '#main'; skip.textContent = '跳到主内容';
    body.insertBefore(skip, body.firstChild);
    host.insertBefore(top, host.firstChild);
    host.insertBefore(side, host.firstChild);

    var toasts = document.createElement('div');
    toasts.className = 'vk-toasts'; toasts.id = 'vkToasts';
    toasts.setAttribute('aria-live', 'polite');
    body.appendChild(toasts);

    // 主题按钮
    paintThemeBtn();
    $('#vkTheme').onclick = function(){
      var t = readTheme() === 'dark' ? 'light' : 'dark';
      try{ localStorage.setItem(THEME_KEY, t); }catch(e){}
      applyTheme(t); paintThemeBtn();
    };

    // 移动端抽屉
    $('#vkMenuBtn').onclick = function(){ toggleDrawer(!side.classList.contains('open')); };
    side.addEventListener('click', function(e){ if(e.target.closest && e.target.closest('a')) toggleDrawer(false); });

    // 返回列表（<1024 二选一版式）
    $('#vkBack').onclick = function(){ showList(); };

    // 搜索
    var sw = $('#vkSearchWrap'), si = $('#vkSearch');
    $('#vkSearchToggle').onclick = function(){ sw.classList.add('open'); si.focus(); };
    si.addEventListener('input', function(){ state.searchFns.forEach(function(f){ f(si.value.trim()); }); });
    si.addEventListener('keydown', function(e){ if(e.key === 'Escape'){ si.value = ''; si.dispatchEvent(new Event('input')); si.blur(); } });
    si.addEventListener('blur', function(){ if(!si.value) sw.classList.remove('open'); });

    // 产品
    $('#vkProduct').onchange = function(){ setProduct($('#vkProduct').value, true); };

    // 全局键盘：/ 聚焦搜索，Esc 关抽屉/菜单
    document.addEventListener('keydown', function(e){
      var t = e.target, typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable);
      if(e.key === '/' && !typing && !e.metaKey && !e.ctrlKey && !e.altKey && !sw.hidden){ e.preventDefault(); sw.classList.add('open'); si.focus(); si.select(); }
      if(e.key === 'Escape'){ closeMenu(); if(side.classList.contains('open')){ toggleDrawer(false); $('#vkMenuBtn').focus(); } }
    });

    // 标签条通用键盘：←/→/Home/End
    document.addEventListener('keydown', function(e){
      var tab = e.target && e.target.closest && e.target.closest('[role="tab"]');
      if(!tab) return;
      var list = tab.closest('[role="tablist"]'); if(!list) return;
      var tabs = Array.prototype.slice.call(list.querySelectorAll('[role="tab"]'));
      var vertical = list.getAttribute('aria-orientation') === 'vertical';
      var i = tabs.indexOf(tab), n = null;
      if(e.key === (vertical ? 'ArrowDown' : 'ArrowRight')) n = (i + 1) % tabs.length;
      else if(e.key === (vertical ? 'ArrowUp' : 'ArrowLeft')) n = (i - 1 + tabs.length) % tabs.length;
      else if(e.key === 'Home') n = 0; else if(e.key === 'End') n = tabs.length - 1;
      if(n == null) return;
      e.preventDefault(); tabs[n].focus(); tabs[n].click();
    });

    if(state.module !== 'feedback') setTimeout(loadPendingCount, 400);
  }

  function paintThemeBtn(){
    var b = $('#vkTheme'); if(!b) return;
    var dark = readTheme() === 'dark';
    b.innerHTML = icon(dark ? 'moon' : 'sun');
    b.title = '主题：' + (dark ? '暗色' : '浅色') + '（点击切到' + (dark ? '浅色' : '暗色') + '）';
    b.setAttribute('aria-label', b.title);
  }

  var scrim = null;
  function toggleDrawer(open){
    var side = $('#vkSide'); if(!side) return;
    side.classList.toggle('open', open);
    $('#vkMenuBtn').setAttribute('aria-expanded', String(open));
    if(open){
      scrim = document.createElement('div'); scrim.className = 'vk-drawer-scrim';
      scrim.onclick = function(){ toggleDrawer(false); };
      document.body.appendChild(scrim);
      var a = side.querySelector('a[aria-current]') || side.querySelector('a'); if(a) a.focus();
    }else if(scrim){ scrim.remove(); scrim = null; }
  }

  /* 数据回流的待审计数：用已有接口算，不加后端接口。拿不到就不显示。 */
  function loadPendingCount(){
    fetch('/admin/feedback/api/agents?days=7').then(function(r){ return r.ok ? r.json() : null; }).then(function(d){
      if(!d || !d.agents) return;
      setNavCount('feedback', d.agents.reduce(function(s, a){ return s + (a.pending || 0); }, 0));
    }).catch(function(){});
  }
  function setNavCount(module, n){
    var el = document.querySelector('[data-count="' + module + '"]'); if(!el) return;
    el.hidden = !n; el.textContent = n ? String(n) : '';
    el.setAttribute('aria-label', n + ' 个待审提案');
    var a = el.parentElement; if(a) a.title = MODULES.filter(function(m){ return m.id === module; })[0].label + (n ? ' · ' + n + ' 个待审提案' : '');
  }

  /* ------------------------------------------------------------ 面包屑 / 列表-详情二选一 */
  function crumb(obj, opts){
    var c = $('#vkCrumb'); if(!c) return;
    var cur = MODULES.filter(function(m){ return m.id === state.module; })[0] || {label:''};
    var back = $('#vkBack');
    c.innerHTML = '';
    if(back) c.appendChild(back);
    var s = document.createElement('span'); s.textContent = cur.label; c.appendChild(s);
    if(obj){
      var sep = document.createElement('span'); sep.className = 'sep'; sep.textContent = '/'; sep.setAttribute('aria-hidden', 'true'); c.appendChild(sep);
      var b = document.createElement('b'); b.textContent = obj; if(!(opts && opts.plain)) b.className = 'mono'; b.title = obj; c.appendChild(b);
    }
  }
  function workspace(){ return $('.vk-workspace'); }
  function showDetail(){ var w = workspace(); if(w) w.setAttribute('data-pane', 'detail'); var b = $('#vkBack'); if(b) b.hidden = false; }
  function showList(){ var w = workspace(); if(w) w.setAttribute('data-pane', 'list'); var b = $('#vkBack'); if(b) b.hidden = true; }

  /* ------------------------------------------------------------ 搜索 / 产品 */
  function onSearch(placeholder, fn){
    var sw = $('#vkSearchWrap'), si = $('#vkSearch');
    if(sw){ sw.hidden = false; si.placeholder = placeholder; }
    state.searchFns.push(fn);
  }
  function setProducts(list, opts){
    opts = opts || {};
    state.products = list;
    var sel = $('#vkProduct'), wrap = $('#vkProductWrap'); if(!sel) return state.product;
    var want = opts.value != null ? opts.value : state.product;
    var vals = list.map(function(p){ return p.value; });
    if(vals.indexOf(want) < 0) want = opts.fallback != null ? opts.fallback : (vals[0] || '');
    sel.innerHTML = list.map(function(p){
      return '<option value="' + esc(p.value) + '"' + (p.value === want ? ' selected' : '') + '>' + esc(p.label) + '</option>';
    }).join('');
    wrap.hidden = false; wrap.classList.toggle("optional", !!opts.optional);
    setProduct(want, false, opts.persist === false);
    return want;
  }
  function setProduct(v, fire, noPersist){
    state.product = v;
    var p = (state.products || []).filter(function(x){ return x.value === v; })[0];
    var wrap = $('#vkProductWrap'); if(wrap) wrap.classList.toggle('child', !!(p && p.child));
    if(!noPersist){ try{ if(v) localStorage.setItem(PRODUCT_KEY, v); }catch(e){} }
    syncUrl('product', v);
    document.querySelectorAll('[data-nav]').forEach(function(a){
      var m = MODULES.filter(function(x){ return x.id === a.dataset.nav; })[0]; if(m) a.href = navHref(m);
    });
    if(fire) state.productFns.forEach(function(f){ f(v); });
  }

  /* ------------------------------------------------------------ toast（§6.12）*/
  function toast(msg, opts){
    opts = opts || {};
    var box = $('#vkToasts'); if(!box){ try{ console.log(msg); }catch(e){} return; }
    var kind = opts.kind || (/^(没成|失败|没保存|打不开|读不出|跑失败|删除失败|导出失败|保存失败|创建失败)/.test(msg) ? 'err' : 'ok');
    var t = document.createElement('div');
    t.className = 'vk-toast ' + kind;
    t.setAttribute('role', kind === 'err' ? 'alert' : 'status');
    t.innerHTML = icon(kind === 'err' ? 'alert' : kind === 'info' ? 'info' : 'check') + '<span class="grow"></span>' +
      (opts.undo ? '<button class="vk-btn vk-btn--ghost vk-btn--sm" data-undo>' + esc(opts.undo.label || '撤销') + '</button>' : '') +
      (kind === 'err' ? '<button class="vk-iconbtn" data-close aria-label="关闭">' + icon('x') + '</button>' : '');
    t.querySelector('.grow').textContent = msg;
    box.appendChild(t);
    var kill = function(){ t.remove(); };
    if(opts.undo) t.querySelector('[data-undo]').onclick = function(){ kill(); opts.undo.fn(); };
    var c = t.querySelector('[data-close]'); if(c) c.onclick = kill;
    if(kind !== 'err') setTimeout(kill, opts.undo ? 8000 : 4000);
    while(box.children.length > 3) box.firstChild.remove();
  }

  /* ------------------------------------------------------------ 确认弹窗（§6.8）
   * confirm({title, body(html), ok, danger, typed, input:{label, placeholder, required, value}})
   * → Promise：取消 resolve(false)；确认 resolve({value})。初始焦点在「取消」。 */
  function confirmDlg(o){
    return new Promise(function(resolve){
      var d = document.createElement('dialog');
      d.className = 'vk-dialog';
      d.setAttribute('aria-labelledby', 'vkDlgT');
      var needTyped = o.typed != null && o.typed !== '';
      d.innerHTML = '<form method="dialog" class="vk-dlg">' +
        '<h2 id="vkDlgT" class="' + (o.danger ? 'danger' : '') + '">' + icon(o.danger ? 'alert' : 'info') + '<span></span></h2>' +
        '<div class="body">' + (o.body || '') + '</div>' +
        (o.input ? '<div class="vk-field"><label for="vkDlgIn"></label><input class="vk-input" id="vkDlgIn" autocomplete="off"></div>' : '') +
        (needTyped ? '<div class="vk-field"><label for="vkDlgTyped">输入 <code class="mono"></code> 确认</label><input class="vk-input mono" id="vkDlgTyped" autocomplete="off" spellcheck="false"></div>' : '') +
        '<div class="vk-dlg-foot"><button type="button" class="vk-btn vk-btn--lg" data-cancel>取消</button>' +
        '<button type="submit" class="vk-btn vk-btn--lg ' + (o.danger ? 'vk-btn--danger' : 'vk-btn--primary') + '" value="ok" data-ok></button></div></form>';
      d.querySelector('h2 span').textContent = o.title || '确认';
      var okBtn = d.querySelector('[data-ok]'); okBtn.textContent = o.ok || '确认';
      var inp = d.querySelector('#vkDlgIn'), typed = d.querySelector('#vkDlgTyped');
      if(inp){ d.querySelector('label[for="vkDlgIn"]').textContent = o.input.label || ''; inp.placeholder = o.input.placeholder || ''; inp.value = o.input.value || ''; }
      if(typed) d.querySelector('label[for="vkDlgTyped"] code').textContent = o.typed;
      function valid(){
        if(typed && typed.value !== String(o.typed)) return false;       // 完全一致，含大小写
        if(inp && o.input.required && !inp.value.trim()) return false;
        return true;
      }
      function refresh(){ okBtn.disabled = !valid(); }
      if(typed) typed.addEventListener('input', refresh);
      if(inp) inp.addEventListener('input', refresh);
      refresh();
      var done = false;
      function finish(v){ if(done) return; done = true; try{ d.close(); }catch(e){} d.remove(); resolve(v); }
      d.querySelector('[data-cancel]').onclick = function(){ finish(false); };
      d.addEventListener('cancel', function(e){ e.preventDefault(); finish(false); });
      d.querySelector('form').addEventListener('submit', function(e){
        e.preventDefault();
        if(!valid()) return;
        finish({value: inp ? inp.value : (typed ? typed.value : '')});
      });
      document.body.appendChild(d);
      d.showModal();
      d.querySelector('[data-cancel]').focus();
    });
  }

  /* ------------------------------------------------------------ ⋯ 溢出菜单 */
  var openMenu = null;
  function closeMenu(){ if(openMenu){ var m = openMenu; openMenu = null; m.el.remove(); document.removeEventListener('mousedown', m.outside, true); if(m.anchor && m.anchor.isConnected) { m.anchor.setAttribute('aria-expanded', 'false'); if(m.refocus) m.anchor.focus(); } } }
  function menu(anchor, items){
    if(openMenu && openMenu.anchor === anchor){ closeMenu(); return; }
    closeMenu();
    var el = document.createElement('div');
    el.className = 'vk-menu'; el.setAttribute('role', 'menu');
    items.forEach(function(it, i){
      var b = document.createElement('button');
      b.type = 'button'; b.setAttribute('role', 'menuitem');
      if(it.danger) b.className = 'danger';
      b.innerHTML = (it.icon ? icon(it.icon) : '') + '<span></span>';
      b.querySelector('span').textContent = it.label;
      b.onclick = function(){ closeMenu(); it.onSelect(); };
      el.appendChild(b);
    });
    el.addEventListener('keydown', function(e){
      var bs = Array.prototype.slice.call(el.querySelectorAll('button')), i = bs.indexOf(document.activeElement);
      if(e.key === 'ArrowDown'){ e.preventDefault(); bs[(i + 1) % bs.length].focus(); }
      else if(e.key === 'ArrowUp'){ e.preventDefault(); bs[(i - 1 + bs.length) % bs.length].focus(); }
      else if(e.key === 'Escape'){ e.preventDefault(); openMenu.refocus = true; closeMenu(); }
      else if(e.key === 'Tab'){ closeMenu(); }
    });
    document.body.appendChild(el);
    var r = anchor.getBoundingClientRect(), w = el.offsetWidth, h = el.offsetHeight;
    var left = Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8));
    var topPos = r.bottom + 4 + h > window.innerHeight - 8 ? Math.max(8, r.top - h - 4) : r.bottom + 4;
    el.style.left = left + 'px'; el.style.top = topPos + 'px';
    var outside = function(e){ if(!el.contains(e.target) && !anchor.contains(e.target)) closeMenu(); };
    document.addEventListener('mousedown', outside, true);
    anchor.setAttribute('aria-expanded', 'true');
    openMenu = {el:el, anchor:anchor, outside:outside};
    el.querySelector('button').focus();
  }

  /* ------------------------------------------------------------ 门票失效 */
  var blocked = false;
  function unauthorized(){
    var t = $('#vkTicket'); if(t){ t.classList.add('bad'); var s = t.querySelector('span'); if(s) s.textContent = '门票无效'; }
    if(blocked || !document.body) return; blocked = true;
    var b = document.createElement('div'); b.className = 'vk-block'; b.setAttribute('role', 'alertdialog'); b.setAttribute('aria-labelledby', 'vkBlockT');
    b.innerHTML = '<div class="vk-card"><h3 id="vkBlockT">门票过期了</h3><p class="hint" style="margin-top:8px">在地址后面加 <code>?k=&lt;门票&gt;</code> 重新进来。页面上已经加载的内容不会丢，但接下来的操作都不会成功。</p></div>';
    document.body.appendChild(b);
  }

  /* 页面共用的 fetch 包装：出错时抛带后端原因的 Error，401 顺手标门票无效 */
  function api(base){
    return async function(path, opt){
      opt = opt || {};
      var r = await fetch(base + path, Object.assign({headers:{'Content-Type':'application/json'}}, opt));
      if(r.status === 401) unauthorized();
      if(!r.ok){
        var d = '';
        try{ d = (await r.json()).detail; }catch(e){}
        if(d && typeof d === 'object') d = (d.message || '') + (d.reasons ? '：' + d.reasons.join('；') : '');
        throw new Error(d || ('HTTP ' + r.status));
      }
      return r.json();
    };
  }

  /* 按钮 loading：锁宽度、换转圈、文字改进行时 */
  async function busy(btn, fn, label){
    if(!btn) return fn();
    var html = btn.innerHTML, w = btn.offsetWidth;
    btn.disabled = true; if(w) btn.style.minWidth = w + 'px';
    btn.innerHTML = '<span class="vk-spin" aria-hidden="true"></span><span>' + esc(label || '进行中…') + '</span>';
    try{ return await fn(); }
    catch(e){ toast('没成：' + e.message, {kind:'err'}); }
    finally{ if(btn.isConnected){ btn.disabled = false; btn.innerHTML = html; btn.style.minWidth = ''; } }
  }

  var VK = {
    esc:esc, icon:icon, toast:toast, confirm:confirmDlg, menu:menu, closeMenu:closeMenu, api:api, busy:busy,
    crumb:crumb, showDetail:showDetail, showList:showList, setNavCount:setNavCount, unauthorized:unauthorized,
    onSearch:onSearch, setProducts:setProducts, onProduct:function(f){ state.productFns.push(f); },
    product:function(){ return state.product; }, theme:readTheme, build:build,
    isNarrow:function(){ try{ return window.matchMedia('(max-width:1023px)').matches; }catch(e){ return false; } },
    isMobile:function(){ try{ return window.matchMedia('(max-width:767px)').matches; }catch(e){ return false; } },
    /* 小写的时间：今天 10:42 / 09-13 / 2026-08-02 */
    when:function(s){
      s = String(s || ''); if(!s) return '—';
      var d = s.replace('T', ' '), day = d.slice(0, 10), now = new Date();
      var today = now.getFullYear() + '-' + String(now.getMonth() + 1).padStart(2, '0') + '-' + String(now.getDate()).padStart(2, '0');
      if(day === today) return '今天 ' + d.slice(11, 16);
      if(day.slice(0, 4) === today.slice(0, 4)) return day.slice(5) + (d.length > 11 ? ' ' + d.slice(11, 16) : '');
      return day;
    }
  };
  if(typeof window !== 'undefined') window.VK = VK;

  if(typeof document !== 'undefined' && document.addEventListener){
    if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', build);
    else build();
  }
})();
