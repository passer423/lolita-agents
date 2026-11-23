/* 单页聊天：多路召回 agent 的前端。加载即渲染，不再依赖 app.js / profile.js。 */
'use strict';

(function () {
  // messages 是当前对话的消息；threads 是账号下的对话列表。
  // 历史只从服务端读，浏览器不再把 history 当唯一来源。
  const state = {
    messages: [], busy: false, abort: null, channels: null,
    account: null, threads: [], threadId: null,
  };

  const esc = (value) => String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

  // 回答里的 [n] 标成小上标；先转义再替换，避免把用户内容当 HTML。
  // 服务端回填的历史消息没有来源可跳，plain=true 时保留普通文本。
  const formatText = (text, plain) => {
    const html = esc(text || '');
    return (plain ? html : html.replace(/\[(\d+)\]/g, '<sup class="cite" data-n="$1">[$1]</sup>'))
      .replace(/\n/g, '<br>');
  };

  // 引用链接必须是可点的 <a>：只渲染纯文本时，读者点的是文字，不是链接。
  // 只给 http(s) 加锚点，内部 ref（real-1 之类）保持纯文本。
  const linkHtml = (url) => {
    const text = esc(url);
    if (!/^https?:\/\//i.test(url || '')) return text;
    return '<a href="' + text + '" target="_blank" rel="noopener noreferrer">' + text + '</a>';
  };

  // 召回通道名 → 人话（过程里别直接甩英文通道名）
  // real 是采集来的他人发言，里头既有网友也有店家公告：不能叫「真实语料」，
  // 也不能一律写「网友讨论」。具体归因由答案里的称谓按来源类型区分。
  const CHANNEL_LABELS = {
    own: '自有知识库',
    real: '过往相关帖文',
    lolibrary: 'Lolibrary 档案',
    dress: '裙子档案',
    xhs: '小红书',
    weibo: '微博',
    web: '网页搜索',
  };

  const REGION_LABELS = {
    JP: '日牌',
    CN: '国牌',
    OTHER: '其他地区',
    UNKNOWN: '地区未识别',
  };

  function toast(message) {
    const node = document.getElementById('toast');
    if (!node) return;
    node.textContent = message;
    node.className = 'toast show';
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => { node.className = 'toast'; }, 2400);
  }

  function view() { return document.getElementById('view'); }

  function stepText(step) {
    if (step.kind === 'recall') {
      const label = CHANNEL_LABELS[step.channel] || step.channel;
      return '· ' + label + ' → ' + (step.empty ? '空' : step.evidence + ' 条');
    }
    if (step.kind === 'skip') {
      const label = CHANNEL_LABELS[step.channel] || step.channel;
      // 带上原因：没有原因的「不可用」会让人以为是网络问题，
      // 而真实原因往往是「搜索没带出微博页」这类策略问题。
      return '· ' + label + ' 不可用' + (step.reason ? '（' + esc(step.reason) + '）' : '');
    }
    if (step.kind === 'route') {
      // 补查由规则决定；error 只表示检索词没提出来，不影响补查
      if (step.error) {
        return '· 检索词提取失败，仍补查：' +
          (step.supplementary || []).map((c) => CHANNEL_LABELS[c] || c).join('、');
      }
      return step.supplementary && step.supplementary.length
        ? '· 追加实时通道：' + step.supplementary.map((c) => CHANNEL_LABELS[c] || c).join('、')
        : '· 不需要实时信息，不补查';
    }
    if (step.kind === 'dress') {
      if (step.pending) return '· 裙子档案：本地没认出，正在让模型识别…';
      const region = REGION_LABELS[step.region] || '地区未识别';
      return '· 裙子档案：' + esc(step.brand || '品牌未识别') + '（' + region + '）' +
        (step.series ? ' · ' + esc(step.series) : '');
    }
    if (step.kind === 'dress_profile') {
      const region = REGION_LABELS[step.region] || '地区未识别';
      return '· 档案整合：' + esc(step.brand || '品牌未识别') + '（' + region + '）';
    }
    if (step.kind === 'dress_search') {
      const via = step.via === 'web' ? '网页反查' : '关键词检索';
      const candidates = (step.candidates || []).join(' / ');
      if (step.matched_title) {
        return '· ' + via + '：' + esc(candidates) + ' → 命中 ' + esc(step.matched_title);
      }
      return '· ' + via + '：' + esc(candidates || '无候选') + ' → ' +
        esc(step.error || '未命中');
    }
    if (step.kind === 'timeout') {
      // 整体预算用尽：说清"等到哪一步"，别让用户以为是某一路挂死
      const waited = step.deadline_s ? Math.round(step.deadline_s) + ' 秒' : '';
      const pending = (step.pending || []).map((c) => CHANNEL_LABELS[c] || c).join('、');
      return '· 整体超时' + (waited ? '（' + waited + '）' : '') +
        (pending ? '，未返回：' + pending : '') + '，先返回已查到的内容';
    }
    return '· ' + esc(step.kind);
  }

  // 首字前的逐通道状态：核心通道先以「查询中…」占位，谁先返回就先更新谁；
  // route 决定补查后再补上实时通道的占位。用户因此不会对着空白气泡等。
  function recallRows(answer) {
    const order = [];
    const rows = {};
    const put = (key, text, done) => {
      if (!rows[key]) order.push(key);
      rows[key] = { text: text, done: done };
    };

    const core = (state.channels && state.channels.core) || ['own', 'real', 'lolibrary'];
    core.forEach((name) => put(name, (CHANNEL_LABELS[name] || name) + ' · 查询中…', false));

    (answer.steps || []).forEach((step) => {
      const text = stepText(step).replace(/^· /, '');
      if (step.kind === 'recall' || step.kind === 'skip') {
        put(step.channel, text, true);
      } else if (step.kind === 'route') {
        put('route', text, true);
        (step.supplementary || []).forEach((name) => {
          put(name, (CHANNEL_LABELS[name] || name) + ' · 查询中…', false);
        });
      } else if (step.kind === 'dress') {
        put('dress', text, true);
      } else if (step.kind === 'dress_profile') {
        put('dress_profile', text, true);
      } else if (step.kind === 'dress_search') {
        put('dress_search_' + order.length, text, true);
      } else if (step.kind === 'timeout') {
        put('timeout', text, true);
      }
    });

    return order.map((key) => rows[key]);
  }

  // 状态行里的动态部分已在 stepText 里转义过，这里直接拼 HTML。
  function renderLiveRecall(wrap, answer) {
    let panel = wrap.querySelector('.recall-live');
    if (!panel) {
      panel = document.createElement('div');
      panel.className = 'recall-live';
      wrap.appendChild(panel);
    }
    panel.innerHTML = '<div class="recall-head">正在召回</div>' +
      recallRows(answer).map((row) =>
        '<div class="recall-row' + (row.done ? ' done' : '') + '">' +
        '<span class="dot"></span>' + row.text + '</div>').join('');
  }

  function markCitations(wrap, bubble, message) {
    const invalid = message.invalidCitations || [];
    const problems = message.citationProblems || [];
    if (!invalid.length && !problems.length) return;

    if (!invalid.length) {
      // 没有越界编号，说明问题是"整段没标来源"——逐条标不出来，只能整体提示
      const warn = document.createElement('div');
      warn.className = 'notice warn cite-warn';
      warn.textContent = '这段回答没有标注来源，事实以下方「参考来源」为准。';
      wrap.insertBefore(warn, bubble.nextSibling);
      return;
    }

    const bad = new Set(invalid.map(String));
    bubble.querySelectorAll('.cite').forEach((node) => {
      if (!bad.has(node.dataset.n)) return;
      node.classList.add('invalid');
      node.title = '这条引用对不上任何来源';
    });

    const warn = document.createElement('div');
    warn.className = 'notice warn cite-warn';
    warn.textContent = '有 ' + invalid.length + ' 处引用对不上来源（' +
      invalid.map((n) => '[' + n + ']').join('、') + '），已标出；事实以下方「参考来源」为准。';
    wrap.insertBefore(warn, bubble.nextSibling);
  }

  function dressPrice(dress) {
    if (dress.price_conflict) return '来源说法不一致';
    if (!dress.price) return '未查到';
    if (dress.price_kind === 'archive_original') return dress.price + '（发售价）';
    if (dress.price_kind === 'social_unverified') return dress.price + '（网友提到，未核实）';
    return dress.price;
  }

  function dressCard(dress) {
    if (!dress || (!dress.brand && !dress.title && !dress.title_cn)) return '';
    const region = REGION_LABELS[dress.region] || '地区未识别';
    const tagClass = dress.region === 'JP' ? ' jp' : (dress.region === 'CN' ? ' cn' : '');
    const title = dress.title
      ? (dress.title_cn ? dress.title + '（' + dress.title_cn + '）' : dress.title)
      : (dress.title_cn || '');
    const rows = [
      ['发售', dress.release_conflict ? '来源说法不一致' : (dress.release || '未查到')],
      ['价格', dressPrice(dress)],
      ['款式', dress.style_conflict ? '来源说法不一致' : (dress.style || '未查到')],
      ['配色', (dress.colors || []).length ? dress.colors.join(' / ') : '未查到'],
    ];
    const notes = [];
    if (dress.verified === false && (dress.source === 'model' || dress.source === 'pending_model')) {
      notes.push('模型识别，未在 Lolibrary 档案中核实');
    }
    if (dress.verified === false && dress.source === 'brand_alias') {
      notes.push('未识别到具体系列');
    }
    if (dress.verified === true && dress.source === 'model_verified') {
      notes.push('已用 Lolibrary 档案核实');
    }
    if ([dress.release_kind, dress.price_kind, dress.style_kind].indexOf('social_unverified') >= 0) {
      notes.push('社媒字段未核实');
    }
    (dress.notes || []).forEach((note) => {
      if (note && notes.indexOf(note) < 0) notes.push(note);
    });
    if ((dress.missing || []).length) notes.push('未查到：' + dress.missing.join('、'));
    return '<div class="dress-card">' +
      '<div class="dress-head"><span class="dress-name">' +
        esc((dress.brand || '品牌未识别') + (title ? ' · ' + title : '')) +
      '</span><span class="dress-tag' + tagClass + '">' + esc(region) + '</span></div>' +
      '<div class="dress-grid">' + rows.map(([label, value]) =>
        '<div class="dress-row"><span>' + label + '</span><b>' + esc(value) + '</b></div>').join('') +
      '</div>' +
      (notes.length ? '<div class="dress-note">' + esc(notes.join('；')) + '</div>' : '') +
      (dress.url ? '<div class="dress-src">来源：' + linkHtml(dress.url) + '</div>' : '') +
      '</div>';
  }

  function messageNode(message) {
    const wrap = document.createElement('div');
    wrap.className = 'msg ' + message.role;

    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.innerHTML = formatText(message.content, message.restored);
    wrap.appendChild(bubble);

    markCitations(wrap, bubble, message);

    if (message.dress) wrap.insertAdjacentHTML('beforeend', dressCard(message.dress));

    if (message.steps && message.steps.length) {
      const details = document.createElement('details');
      details.className = 'steps';
      const recallCount = message.steps.filter((step) => step.kind !== 'route').length;
      details.innerHTML = '<summary>召回过程（' + recallCount + ' 路）</summary>' +
        message.steps.map((step) => '<div class="step">' + stepText(step) + '</div>').join('');
      wrap.appendChild(details);
    }

    if (message.evidence && message.evidence.length) {
      // 默认收起：回答是主体，参考资料要看再展开；点 [n] 会自动展开并定位。
      const evidence = document.createElement('details');
      evidence.className = 'evidence';
      evidence.innerHTML = '<summary>参考来源（' + message.evidence.length + '）</summary>' +
        message.evidence.map((item) =>
          '<div class="ev" data-n="' + item.n + '"><span class="ev-n">[' + item.n + ']</span>' +
          '<span class="ev-title">' + esc(CHANNEL_LABELS[item.channel] || item.channel) + ' · ' +
          esc(item.title) + '</span>' +
          (item.url ? '<div class="ev-link">' + linkHtml(item.url) + '</div>' : '') +
          '<div class="ev-text">' + esc(item.text) + '</div></div>').join('');
      wrap.appendChild(evidence);

      // 点回答里的 [n] 跳到对应来源（先展开，否则定位不到）
      wrap.querySelectorAll('.cite[data-n]').forEach((node) => {
        node.addEventListener('click', () => {
          const target = wrap.querySelector('.ev[data-n="' + node.dataset.n + '"]');
          if (!target) return;
          evidence.open = true;
          target.scrollIntoView({ behavior: 'smooth', block: 'center' });
          target.classList.add('flash');
          setTimeout(() => target.classList.remove('flash'), 1200);
        });
      });
    }
    if (message.role === 'assistant' && message.followSuggestion) {
      const suggest = document.createElement('div');
      suggest.className = 'follow-suggest';
      if (message.followAdded) {
        suggest.innerHTML = '<span>已加入关注项：' +
          esc(message.followSuggestion.subject) + '</span>';
      } else {
        suggest.innerHTML = '<span>' + esc(message.followSuggestion.reason || '需要我持续关注这条吗？') + '</span>' +
          '<button class="ghost sm" data-follow="add">持续关注</button>' +
          '<button class="ghost sm" data-follow="skip">不用</button>';
        suggest.querySelector('[data-follow="add"]').addEventListener('click', () => {
          addFollowFromSuggestion(message);
        });
        suggest.querySelector('[data-follow="skip"]').addEventListener('click', () => {
          message.followSuggestion = null;
          paintMessages(document.getElementById('chat-list'));
        });
      }
      wrap.appendChild(suggest);
    }
    return wrap;
  }

  function paintMessages(host) {
    host.innerHTML = '';
    if (!state.messages.length) {
      host.innerHTML = '<div class="hint" style="text-align:center;padding:18px 0">' +
        '<b>Lolita 圈层问答助手</b><br>' +
        '把微博真实语料、Lolibrary 档案和自有知识库整理成可检索的知识，只回答 Lo 裙相关的问题。<br>' +
        '问具体裙子会先判断日牌 / 国牌再分流查证；答案带引用、可点开看原文，查不到会直接说查不到。</div>';
      return;
    }
    state.messages.forEach((message) => host.appendChild(messageNode(message)));
    host.scrollTop = host.scrollHeight;
  }

  async function ask(question, ui) {
    if (state.busy) return;
    state.busy = true;
    ui.input.disabled = true;
    ui.send.disabled = true;
    if (ui.stop) {
      ui.stop.style.display = '';
      ui.stop.disabled = false;
    }

    const userMessage = { role: 'user', content: question };
    state.messages.push(userMessage);

    const answer = { role: 'assistant', content: '', steps: [], evidence: [] };
    state.messages.push(answer);
    paintMessages(ui.list);

    const wrap = ui.list.lastElementChild;
    const bubble = wrap.querySelector('.bubble');
    let steps = [];
    const started = performance.now();

    // 「停止」不只是前端断开：服务端在下一个 chunk 边界收尾，不再消费上游结果。
    const controller = new AbortController();
    state.abort = controller;

    try {
      const response = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json; charset=utf-8' },
        // 不传 history：上下文由服务端按 conversation_id 从库里取。
        // 首问 conversation_id 为 null，服务端会新建对话并在第一帧回传 id。
        body: JSON.stringify({ message: question, conversation_id: state.threadId }),
        signal: controller.signal,
      });

      if (!response.ok) {
        let detail = response.status + ' ' + response.statusText;
        try { const data = await response.json(); if (data.detail) detail = data.detail; } catch (_) { /* 非 JSON */ }
        const error = new Error(detail);
        error.status = response.status;
        throw error;
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let cut;
        while ((cut = buffer.indexOf('\n\n')) >= 0) {
          const frame = buffer.slice(0, cut);
          buffer = buffer.slice(cut + 2);
          const line = frame.split('\n').find((item) => item.indexOf('data: ') === 0);
          if (!line) continue;   // 心跳是 ": keep-alive" 注释帧，忽略
          let event;
          try { event = JSON.parse(line.slice(6)); } catch (_) { continue; }

          if (event.type === 'conversation') {
            // 新对话的 id/首问标题由服务端给，前端只负责挂到列表并高亮。
            state.threadId = event.id;
            upsertThread({
              id: event.id, title: event.title || '新对话',
              created_at: event.created_at, updated_at: event.updated_at,
              message_count: event.message_count || 0,
            });
            renderThreads();
          } else if (event.type === 'step') {
            steps.push(event);
            answer.steps = steps;
            if (!answer.content) {
              // 首字前：逐通道展示「查询中 / 几条 / 不可用（原因）」。
              bubble.innerHTML = '<span class="thinking">正在召回…</span>';
              renderLiveRecall(wrap, answer);
            }
          } else if (event.type === 'delta') {
            const panel = wrap.querySelector('.recall-live');
            if (panel) panel.remove();
            answer.content += event.text;
            bubble.innerHTML = formatText(answer.content);
            ui.list.scrollTop = ui.list.scrollHeight;
          } else if (event.type === 'replace') {
            const panel = wrap.querySelector('.recall-live');
            if (panel) panel.remove();
            answer.content = event.text;
            bubble.innerHTML = formatText(answer.content);
          } else if (event.type === 'final') {
            answer.content = event.answer;
            answer.steps = event.steps || steps;
            answer.evidence = event.evidence || [];
            answer.refused = !!event.refused;
            answer.citationProblems = event.citation_problems || [];
            answer.invalidCitations = event.invalid_citations || [];
            answer.followSuggestion = event.follow_suggestion || null;
            answer.dress = event.dress || null;
          } else if (event.type === 'error') {
            throw new Error(event.detail || '流式失败');
          }
        }
      }

      answer.ms = Math.round(performance.now() - started);
      paintMessages(ui.list);
      refreshThreads();          // 更新标题/条数/排序，不打断当前气泡
    } catch (error) {
      if (error && error.name === 'AbortError') {
        answer.stopped = true;
        answer.content = (answer.content ? answer.content + '\n\n' : '') + '（已停止生成）';
      } else if (error && error.status === 401) {
        // 登录态过期：不要在页面里留一个「出错了」的气泡，直接回登录页。
        state.busy = false;
        state.abort = null;
        toast('登录已过期，请重新登录');
        renderAuth('login');
        return;
      } else {
        answer.error = true;
        answer.content = '出错了：' + error.message;
      }
      paintMessages(ui.list);
    } finally {
      state.busy = false;
      state.abort = null;
      ui.input.disabled = false;
      ui.send.disabled = false;
      if (ui.stop) {
        ui.stop.style.display = 'none';
        ui.stop.disabled = true;
      }
      ui.input.focus();
    }
  }

  // ---------------------------------------------------------------- 工具

  async function getJSON(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) {
      let detail = response.status + ' ' + response.statusText;
      try { const data = await response.json(); if (data.detail) detail = data.detail; } catch (_) { /* 非 JSON */ }
      const error = new Error(detail);
      error.status = response.status;   // 调用方靠它区分「登录过期」和其它失败
      throw error;
    }
    return response.json();
  }

  function authExpired(error) {
    if (error && error.status === 401) {
      toast('登录已过期，请重新登录');
      renderAuth('login');
      return true;
    }
    return false;
  }

  // ---------------------------------------------------------------- 关注项（D2 形态甲）
  // 自动检查只在**每天第一次打开页面**时做一次；手动「检查更新」随时可用。
  // 后端不常驻定时任务，页面关掉就停。
  const FOLLOW_CHECK_KEY = 'follow_checked_on';   // 存本地日期（YYYY-MM-DD）
  let followState = null;
  let followBusy = false;

  function todayKey() {
    const now = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    return now.getFullYear() + '-' + pad(now.getMonth() + 1) + '-' + pad(now.getDate());
  }

  const sourceLabel = (name) => {
    const item = ((followState && followState.sources) || []).find((source) => source.name === name);
    return item ? item.label : (CHANNEL_LABELS[name] || name);
  };

  function formatTime(value) {
    if (!value) return '';
    const date = new Date(value);
    if (isNaN(date.getTime())) return value;
    const pad = (n) => String(n).padStart(2, '0');
    return (date.getMonth() + 1) + '-' + pad(date.getDate()) + ' ' +
      pad(date.getHours()) + ':' + pad(date.getMinutes());
  }

  function renderFollows() {
    const unreadNode = document.getElementById('follow-unread');
    const listNode = document.getElementById('follow-list');
    const eventsNode = document.getElementById('follow-events');
    const panel = document.getElementById('follow-panel');
    if (!listNode || !followState) return;

    const follows = followState.follows || [];
    const events = followState.events || [];

    // 关注项不是固定区块：没有关注项时整块不出现（避免空面板 + 手动入口）。
    if (panel) panel.style.display = follows.length ? '' : 'none';

    if (unreadNode) {
      unreadNode.textContent = followState.unread ? followState.unread + ' 条更新' : '';
      unreadNode.className = 'badge' + (followState.unread ? ' on' : '');
      unreadNode.style.display = followState.unread ? '' : 'none';
    }

    eventsNode.innerHTML = events.length
      ? events.map((event) =>
          '<div class="follow-event' + (event.read ? '' : ' unread') + '">' +
            '<div class="head"><b>' + esc(event.subject || '已删除的关注项') + '</b>' +
              '<span>' + esc(sourceLabel(event.source)) + '</span>' +
              '<span>' + esc(formatTime(event.created_at)) + '</span></div>' +
            '<div>' + esc(event.summary) + '</div>' +
          '</div>').join('')
      : '<div class="follow-empty">还没有更新。第一次检查只记录基线，之后才开始对比。</div>';

    const results = {};
    (followState.lastResults || []).forEach((item) => { results[item.follow_id] = item; });

    listNode.innerHTML = follows.map((follow) => {
      const result = results[follow.id];
      let detail = '';
      if (result && result.status !== 'unchanged') {
        const text = result.status === 'updated' ? result.detail
          : result.status === 'unavailable' ? '通道不可用：' + result.detail
          : result.status === 'baseline' ? result.detail
          : '检查失败：' + result.detail;
        detail = '<div class="meta">' + esc(text) + '</div>';
      }
      return '<div class="follow-row">' +
        '<div class="grow"><div class="name">' + esc(follow.subject) + '</div>' +
        '<div class="meta">' + esc(sourceLabel(follow.source)) + ' · 上次检查 ' +
          esc(formatTime(follow.updated_at)) + '</div>' + detail + '</div>' +
        '<button class="follow-del" data-del="' + follow.id + '" title="取消关注">×</button>' +
      '</div>';
    }).join('');

    listNode.querySelectorAll('[data-del]').forEach((button) => {
      button.addEventListener('click', async () => {
        if (!window.confirm('取消关注这个关键词？')) return;
        try {
          followState = await getJSON('/api/follows/' + button.dataset.del, { method: 'DELETE' });
          renderFollows();
        } catch (error) {
          toast('删除失败：' + error.message);
        }
      });
    });

    if (followState.unread) {
      const panel = document.getElementById('follow-panel');
      if (panel) panel.open = true;
    }
  }

  // 用户在回答下方点「持续关注」才建关注项——不做手动输入入口。
  async function addFollowFromSuggestion(message) {
    const suggestion = message.followSuggestion;
    if (!suggestion) return;
    try {
      const response = await fetch('/api/follows', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json; charset=utf-8' },
        body: JSON.stringify({
          subject: suggestion.subject,
          source: suggestion.source || 'web',
        }),
      });
      if (response.status === 409) {
        // 已经关注过，不是失败：刷新快照就好
        followState = await getJSON('/api/follows');
      } else if (!response.ok) {
        let detail = response.status + ' ' + response.statusText;
        try {
          const data = await response.json();
          if (data.detail) detail = data.detail;
        } catch (_) { /* 非 JSON */ }
        throw new Error(detail);
      } else {
        followState = await response.json();
      }
      message.followAdded = true;
      renderFollows();
      paintMessages(document.getElementById('chat-list'));
      toast('已加入关注：' + suggestion.subject);
    } catch (error) {
      toast('关注失败：' + error.message);
    }
  }

  async function checkFollows(auto) {
    if (followBusy) return;
    if (!followState || !(followState.follows || []).length) return;
    if (document.visibilityState !== 'visible') return;

    followBusy = true;
    const statusNode = document.getElementById('follow-status');
    const button = document.getElementById('follow-check');
    if (statusNode) statusNode.textContent = '正在检查…';
    if (button) button.disabled = true;

    try {
      const data = await getJSON('/api/follows/check', { method: 'POST' });
      const result = data.job_id ? await followJob(data.job_id, statusNode) : data;
      applyScanResult(result);
      if (auto && result.updated) toast('有关注项更新了');
    } catch (error) {
      if (statusNode) statusNode.textContent = '检查失败：' + error.message;
    } finally {
      followBusy = false;
      if (button) button.disabled = false;
    }
  }

  // 后台任务：入队后轮询到终态。页面切到后台时放慢节奏，不做「假进度」——
  // 文案里的数字全部来自服务端的真实计数（done/total），没有就只写状态。
  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function followJobText(job) {
    if (job.status === 'queued') return '排队中…';
    const progress = job.progress || {};
    if (progress.total) {
      return '正在检查 ' + (progress.done || 0) + '/' + progress.total +
        (progress.current ? '：' + progress.current : '');
    }
    const phases = { starting: '准备中…', building: '正在重建索引…', reloading: '正在重载索引…' };
    return phases[progress.phase] || '正在检查…';
  }

  async function followJob(jobId, statusNode) {
    const deadline = Date.now() + 5 * 60 * 1000;
    while (Date.now() < deadline) {
      const job = await getJSON('/api/jobs/' + jobId);
      if (statusNode) statusNode.textContent = followJobText(job);
      if (job.status === 'succeeded') return job.result || {};
      if (job.status === 'failed') throw new Error(job.error || '后台任务失败');
      if (job.status === 'interrupted') throw new Error(job.error || '后台任务被中断');
      await sleep(document.visibilityState === 'visible' ? 800 : 3000);
    }
    throw new Error('等待后台任务超时，稍后刷新页面可查看结果');
  }

  function applyScanResult(data) {
    const statusNode = document.getElementById('follow-status');
    if (data.busy) {
      if (statusNode) statusNode.textContent = '已有一轮检查在进行，稍后自动刷新。';
      return;
    }
    localStorage.setItem(FOLLOW_CHECK_KEY, todayKey());
    followState = {
      ...followState,
      events: data.events,
      unread: data.unread,
      lastResults: data.results || [],
    };
    renderFollows();

    const results = data.results || [];
    const updated = results.filter((item) => item.status === 'updated').length;
    const unavailable = results.filter((item) => item.status === 'unavailable').length;
    const failed = results.filter((item) => item.status === 'error').length;
    const parts = [];
    if (updated) parts.push(updated + ' 条有更新');
    if (unavailable) parts.push(unavailable + ' 条通道不可用');
    if (failed) parts.push(failed + ' 条检查失败');
    if (!parts.length) parts.push('没有变化');
    if (statusNode) {
      if (data.interrupted) parts.push('服务中断，剩余项未检查');
      statusNode.textContent = '检查完成（' + formatTime(data.checked_at) + '）：' + parts.join('，');
    }
    data.updated = updated;
    return updated;
  }

  async function resumeScan() {
    // 刷新页面不该把正在跑的后台扫描「弄丢」：先看队列里有没有活跃任务。
    if (followBusy || !(followState.follows || []).length) return false;
    let active = null;
    try {
      const feed = await getJSON('/api/jobs?kind=follows.scan&limit=5');
      active = (feed.jobs || []).find(
        (job) => job.status === 'queued' || job.status === 'running'
      );
    } catch (_) {
      return false;
    }
    if (!active) return false;

    followBusy = true;
    const statusNode = document.getElementById('follow-status');
    const button = document.getElementById('follow-check');
    if (button) button.disabled = true;
    try {
      applyScanResult(await followJob(active.job_id, statusNode));
    } catch (error) {
      if (statusNode) statusNode.textContent = '检查失败：' + error.message;
    } finally {
      followBusy = false;
      if (button) button.disabled = false;
    }
    return true;
  }

  async function loadFollows() {
    try {
      followState = await getJSON('/api/follows');
      renderFollows();
      const resumed = await resumeScan();
      const checkedOn = localStorage.getItem(FOLLOW_CHECK_KEY);
      if (!resumed && (followState.follows || []).length && checkedOn !== todayKey()) {
        checkFollows(true);
      }
    } catch (error) {
      if (authExpired(error)) return;
      const list = document.getElementById('follow-list');
      if (list) list.innerHTML = '<div class="notice warn">关注项载入失败：' + esc(error.message) + '</div>';
    }
  }

  // ---------------------------------------------------------------- 登录 / 注册

  function renderAuth(mode) {
    const root = view();
    mode = mode === 'register' ? 'register' : 'login';
    state.account = null;
    state.threads = [];
    state.threadId = null;
    state.messages = [];

    root.innerHTML =
      '<div class="card chat-card auth-card">' +
        '<h2 class="auth-title">' + (mode === 'register' ? '注册账号' : '登录') + '</h2>' +
        '<p class="hint">账号用来保存你的对话与关注项，换浏览器登录同一账号即可恢复；' +
          '未登录时看不到任何历史。</p>' +
        '<div class="auth-tabs">' +
          '<button class="chip' + (mode === 'login' ? ' on' : '') + '" data-auth-mode="login">登录</button>' +
          '<button class="chip' + (mode === 'register' ? ' on' : '') + '" data-auth-mode="register">注册</button>' +
        '</div>' +
        '<label class="auth-field"><span>用户名</span>' +
          '<input id="auth-user" type="text" autocomplete="username" maxlength="32" ' +
            'placeholder="3–32 个字符，中文/字母/数字"></label>' +
        '<label class="auth-field"><span>密码</span>' +
          '<input id="auth-pass" type="password" autocomplete="' +
            (mode === 'register' ? 'new-password' : 'current-password') + '" ' +
            'placeholder="至少 8 位"></label>' +
        '<button class="primary auth-submit" id="auth-submit">' +
          (mode === 'register' ? '注册并登录' : '登录') + '</button>' +
        '<div class="auth-msg hint" id="auth-msg"></div>' +
      '</div>';

    const user = document.getElementById('auth-user');
    const pass = document.getElementById('auth-pass');
    const msg = document.getElementById('auth-msg');
    const submit = document.getElementById('auth-submit');

    root.querySelectorAll('[data-auth-mode]').forEach((button) => {
      button.addEventListener('click', () => {
        const keep = user.value;
        renderAuth(button.dataset.authMode);
        // 切模式时保留已输入的用户名，少打一遍
        const node = document.getElementById('auth-user');
        if (node) { node.value = keep; node.focus(); }
      });
    });

    async function doSubmit() {
      const value = user.value.trim();
      if (!value || !pass.value) { msg.textContent = '请填写用户名和密码'; return; }
      submit.disabled = true;
      msg.textContent = mode === 'register' ? '正在注册…' : '正在登录…';
      try {
        const response = await fetch('/api/auth/' + mode, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json; charset=utf-8' },
          body: JSON.stringify({ username: value, password: pass.value }),
        });
        let data = {};
        try { data = await response.json(); } catch (_) { /* 非 JSON */ }
        if (!response.ok) {
          msg.textContent = data.detail || ('失败：' + response.status);
          submit.disabled = false;
          return;
        }
        state.account = data.account || null;
        boot();
      } catch (error) {
        msg.textContent = '网络错误：' + error.message;
        submit.disabled = false;
      }
    }

    submit.addEventListener('click', doSubmit);
    pass.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); doSubmit(); }
    });
    user.focus();
  }

  // ---------------------------------------------------------------- 对话列表

  function threadTitle(thread) {
    return (thread && thread.title) || '新对话';
  }

  function upsertThread(thread) {
    const index = state.threads.findIndex((item) => item.id === thread.id);
    if (index >= 0) {
      state.threads[index] = Object.assign({}, state.threads[index], thread);
    } else {
      state.threads.unshift(thread);
    }
  }

  function setSidebar(open) {
    const side = document.getElementById('thread-side');
    const backdrop = document.getElementById('thread-backdrop');
    const toggle = document.getElementById('thread-toggle');
    if (!side) return;
    side.classList.toggle('open', open);
    if (backdrop) backdrop.classList.toggle('show', open);
    if (toggle) toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  }

  function renderThreads() {
    const bar = document.getElementById('thread-bar');
    if (!bar) return;
    // 空态不渲染占位文字或空白块：列表为空时侧栏只剩「新对话」按钮。
    if (!state.threads.length) {
      bar.innerHTML = '';
      return;
    }
    bar.innerHTML = state.threads.map((thread) =>
      '<div class="thread-item' + (thread.id === state.threadId ? ' on' : '') + '">' +
        '<button class="thread-open" data-thread="' + thread.id + '" title="' +
          esc(threadTitle(thread)) + '">' + esc(threadTitle(thread)) + '</button>' +
        '<button class="thread-del" data-del-thread="' + thread.id + '" ' +
          'title="删除这个对话">×</button>' +
      '</div>').join('');

    bar.querySelectorAll('[data-thread]').forEach((button) => {
      button.addEventListener('click', () => openThread(Number(button.dataset.thread)));
    });
    bar.querySelectorAll('[data-del-thread]').forEach((button) => {
      button.addEventListener('click', (event) => {
        event.stopPropagation();
        deleteThread(Number(button.dataset.delThread));
      });
    });
  }

  async function openThread(id) {
    if (state.busy) { toast('正在生成，停止后再切换对话'); return; }
    if (id === state.threadId) return;
    try {
      const data = await getJSON('/api/threads/' + id + '/messages?limit=100');
      state.threadId = id;
      state.messages = (data.messages || []).map((message) => ({
        role: message.role, content: message.content, restored: true,
      }));
      if (data.thread) upsertThread(data.thread);
      paintMessages(document.getElementById('chat-list'));
      renderThreads();
      setSidebar(false);   // 窄屏抽屉：选完对话自动收起
      const input = document.getElementById('chat-text');
      if (input) input.focus();
    } catch (error) {
      if (!authExpired(error)) toast('打开对话失败：' + error.message);
    }
  }

  async function newThread() {
    if (state.busy) { toast('正在生成，停止后再新建对话'); return; }
    try {
      const data = await getJSON('/api/threads', { method: 'POST' });
      const thread = data.thread;
      state.threadId = thread.id;
      state.messages = [];
      upsertThread(thread);
      state.threads.sort((a, b) =>
        String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
      paintMessages(document.getElementById('chat-list'));
      renderThreads();
      setSidebar(false);   // 窄屏抽屉：新建后直接回到聊天区
      const input = document.getElementById('chat-text');
      if (input) input.focus();
      toast('已新建对话');
    } catch (error) {
      if (!authExpired(error)) toast('新建对话失败：' + error.message);
    }
  }

  async function deleteThread(id) {
    if (state.busy) { toast('正在生成，停止后再删除对话'); return; }
    if (!window.confirm('删除这个对话？聊天记录会一起删掉。')) return;
    try {
      await getJSON('/api/threads/' + id, { method: 'DELETE' });
      state.threads = state.threads.filter((thread) => thread.id !== id);
      if (state.threadId === id) {
        state.threadId = null;
        state.messages = [];
        if (state.threads.length) {
          await openThread(state.threads[0].id);
        } else {
          paintMessages(document.getElementById('chat-list'));
        }
      }
      renderThreads();
    } catch (error) {
      if (!authExpired(error)) toast('删除对话失败：' + error.message);
    }
  }

  async function refreshThreads() {
    try {
      const data = await getJSON('/api/threads');
      state.threads = data.threads || [];
      renderThreads();
    } catch (_) { /* 列表刷新失败不影响当前对话 */ }
  }

  async function logout() {
    // 生成中登出：先停掉流，别让后台请求在登录页上继续改状态。
    if (state.abort) state.abort.abort();
    state.busy = false;
    state.abort = null;
    try { await fetch('/api/auth/logout', { method: 'POST' }); } catch (_) { /* 网络失败也清本地 */ }
    renderAuth('login');
  }

  // ---------------------------------------------------------------- 主界面

  async function renderChat() {
    const root = view();
    let status = null;
    try {
      const response = await fetch('/api/chat/status');
      status = await response.json();
    } catch (error) {
      root.innerHTML = '<div class="card"><div class="notice warn">助手状态未知：' + esc(error.message) + '</div></div>';
      return;
    }

    if (!status.enabled) {
      root.innerHTML = '<div class="card"><h2>助手还没启用</h2>' +
        '<div class="notice warn">' + esc(status.hint || '需要配置模型') + '</div>' +
        '<div class="auth-actions"><button class="ghost sm" id="chat-logout">登出</button></div></div>';
      document.getElementById('chat-logout').addEventListener('click', logout);
      return;
    }

    const username = (state.account && state.account.username) || '';
    // 顶栏只展示产品名，不暴露真实模型标识；展示名由服务端配置下发。
    const displayName = (status && status.display_name) || 'lolitaagent';
    // 问答是主区域，放最上面；关注项是辅助功能，折叠在下面，
    // 不占首屏、也不需要用户先滑过设置才能提问。
    root.innerHTML =
      '<div class="chat-shell">' +
        '<aside class="thread-side card" id="thread-side" aria-label="对话列表">' +
          '<button class="primary thread-new" id="thread-new">新对话</button>' +
          '<div class="thread-list" id="thread-bar"></div>' +
        '</aside>' +
        '<div class="chat-main">' +
          '<div class="card chat-card">' +
            '<div class="chat-head">' +
              '<span class="chat-model">' + esc(displayName) +
                (username ? ' · ' + esc(username) : '') + '</span>' +
              '<div class="chat-actions">' +
                '<button class="ghost sm thread-toggle" id="thread-toggle" ' +
                  'aria-expanded="false" aria-controls="thread-side">对话</button>' +
                '<button class="ghost sm" id="chat-stop" style="display:none" disabled>停止</button>' +
                '<button class="ghost sm" id="chat-logout">登出</button>' +
              '</div>' +
            '</div>' +
            '<div class="chat-list" id="chat-list"></div>' +
            '<div class="chat-input">' +
              '<textarea id="chat-text" rows="1" maxlength="300" ' +
                'placeholder="例：蕾丝和真丝能机洗吗？"></textarea>' +
              '<button class="primary" id="chat-send">发送</button>' +
            '</div>' +
            '<div class="chips chat-chips">' +
              ['蕾丝和真丝能机洗吗？', 'AP 最近有什么新款？', 'Baby 缎带与齿轮 预约价多少？'].map((text) =>
                '<button class="chip" data-q="' + esc(text) + '">' + esc(text) + '</button>').join('') +
            '</div>' +
          '</div>' +
          '<details class="card fold" id="follow-panel" style="display:none">' +
            '<summary>关注项 <span class="badge" id="follow-unread" style="display:none"></span>' +
              '<span class="fold-hint">每天第一次打开页面时检查</span></summary>' +
            '<div class="fold-body">' +
              '<div class="follow-events" id="follow-events"></div>' +
              '<div class="follow-list" id="follow-list"></div>' +
              '<div class="follow-actions">' +
                '<button class="ghost sm" id="follow-check">检查更新</button>' +
                '<button class="ghost sm" id="follow-read">全部已读</button>' +
                '<span class="hint" id="follow-status"></span>' +
              '</div>' +
            '</div>' +
          '</details>' +
        '</div>' +
      '</div>' +
      '<div class="thread-backdrop" id="thread-backdrop"></div>';

    state.channels = status.channels;

    const ui = {
      list: document.getElementById('chat-list'),
      input: document.getElementById('chat-text'),
      send: document.getElementById('chat-send'),
      stop: document.getElementById('chat-stop'),
    };

    const send = () => {
      const question = ui.input.value.trim();
      if (!question) { toast('先写一个问题'); return; }
      ui.input.value = '';
      ask(question, ui);
    };

    ui.send.addEventListener('click', send);
    ui.stop.addEventListener('click', () => {
      if (!state.abort) return;
      ui.stop.disabled = true;
      state.abort.abort();
    });
    ui.input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        send();
      }
    });
    root.querySelectorAll('[data-q]').forEach((button) => {
      button.addEventListener('click', () => {
        ui.input.value = button.dataset.q;
        send();
      });
    });
    document.getElementById('thread-new').addEventListener('click', newThread);
    document.getElementById('chat-logout').addEventListener('click', logout);
    document.getElementById('thread-toggle').addEventListener('click', () => {
      const side = document.getElementById('thread-side');
      setSidebar(!(side && side.classList.contains('open')));
    });
    document.getElementById('thread-backdrop').addEventListener('click', () => setSidebar(false));

    // 首屏：有对话就打开最近一个，没有就留空等首问自动新建。
    renderThreads();
    if (!state.threadId && state.threads.length) {
      openThread(state.threads[0].id);
    } else {
      paintMessages(ui.list);
    }
    // 关注项（形态甲）：打开页面时载入；有历史关注项才显示面板，不提供手动新增
    loadFollows();

    // iOS Safari：键盘弹起时 visualViewport 变矮、布局视口不变，dvh 不一定跟着缩。
    // 输入框有焦点时把它最小幅度滚进可视区，避免被键盘挡住（Android 上是无害的空操作）。
    if (window.visualViewport) {
      window.visualViewport.addEventListener('resize', () => {
        if (document.activeElement !== ui.input) return;
        ui.input.scrollIntoView({ block: 'nearest' });
      });
    }

    document.getElementById('follow-check').addEventListener('click', () => checkFollows(false));
    document.getElementById('follow-read').addEventListener('click', async () => {
      try {
        followState = await getJSON('/api/follows/events/read', { method: 'POST' });
        renderFollows();
        const statusNode = document.getElementById('follow-status');
        if (statusNode) statusNode.textContent = '已全部标为已读';
      } catch (error) {
        if (!authExpired(error)) toast('操作失败：' + error.message);
      }
    });

    // 不做轮询：自动检查已在 loadFollows() 里按「每日首次打开」触发，
    // 页面一直开着也不再重复请求；要刷新就点「检查更新」。
    paintMessages(ui.list);
    ui.input.focus();
  }

  async function boot() {
    let me = null;
    try {
      const response = await fetch('/api/auth/me');
      if (response.status === 401) { renderAuth('login'); return; }
      me = await response.json();
      if (!response.ok) throw new Error((me && me.detail) || ('失败：' + response.status));
    } catch (error) {
      view().innerHTML = '<div class="card"><div class="notice warn">登录状态未知：' +
        esc(error.message) + '</div></div>';
      return;
    }
    state.account = me.account || null;
    state.threadId = null;
    state.messages = [];
    try {
      const data = await getJSON('/api/threads');
      state.threads = data.threads || [];
    } catch (error) {
      if (authExpired(error)) return;
      state.threads = [];
    }
    renderChat();
  }

  boot();
})();
