/* 单页聊天：多路召回 agent 的前端。加载即渲染，不再依赖 app.js / profile.js。 */
'use strict';

(function () {
  const MAX_HISTORY = 8;          // 与服务端 writer.MAX_HISTORY_TURNS 对齐

  const state = { messages: [], busy: false };

  const esc = (value) => String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

  // 回答里的 [n] 标成小上标；先转义再替换，避免把用户内容当 HTML
  const formatText = (text) => esc(text || '')
    .replace(/\[(\d+)\]/g, '<sup class="cite" data-n="$1">[$1]</sup>')
    .replace(/\n/g, '<br>');

  // 召回通道名 → 人话（过程里别直接甩英文通道名）
  const CHANNEL_LABELS = {
    own: '自有知识库',
    real: '真实语料',
    lolibrary: 'Lolibrary 档案',
    xhs: '小红书',
    weibo: '微博',
    web: '网页搜索',
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
      return '· ' + label + ' 不可用';
    }
    if (step.kind === 'route') {
      if (step.error) return '· 补查决策失败，仅用核心通道';
      return step.supplementary && step.supplementary.length
        ? '· 追加实时通道：' + step.supplementary.map((c) => CHANNEL_LABELS[c] || c).join('、')
        : '· 核心通道已覆盖，无需补查';
    }
    return '· ' + esc(step.kind);
  }

  function messageNode(message) {
    const wrap = document.createElement('div');
    wrap.className = 'msg ' + message.role;

    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.innerHTML = formatText(message.content);
    wrap.appendChild(bubble);

    if (message.steps && message.steps.length) {
      const details = document.createElement('details');
      details.className = 'steps';
      const recallCount = message.steps.filter((step) => step.kind !== 'route').length;
      details.innerHTML = '<summary>召回过程（' + recallCount + ' 路）</summary>' +
        message.steps.map((step) => '<div class="step">' + stepText(step) + '</div>').join('');
      wrap.appendChild(details);
    }

    if (message.evidence && message.evidence.length) {
      const evidence = document.createElement('div');
      evidence.className = 'evidence';
      evidence.innerHTML = '<div class="ev-head">参考来源</div>' + message.evidence.map((item) =>
        '<div class="ev" data-n="' + item.n + '"><span class="ev-n">[' + item.n + ']</span>' +
        '<span class="ev-title">' + esc(CHANNEL_LABELS[item.channel] || item.channel) + ' · ' +
        esc(item.title) + '</span>' +
        (item.url ? '<div class="ev-link">' + esc(item.url) + '</div>' : '') +
        '<div class="ev-text">' + esc(item.text) + '</div></div>').join('');
      wrap.appendChild(evidence);

      // 点回答里的 [n] 跳到对应来源
      wrap.querySelectorAll('.cite[data-n]').forEach((node) => {
        node.addEventListener('click', () => {
          const target = wrap.querySelector('.ev[data-n="' + node.dataset.n + '"]');
          if (!target) return;
          target.scrollIntoView({ behavior: 'smooth', block: 'center' });
          target.classList.add('flash');
          setTimeout(() => target.classList.remove('flash'), 1200);
        });
      });
    }
    return wrap;
  }

  function paintMessages(host) {
    host.innerHTML = '';
    if (!state.messages.length) {
      host.innerHTML = '<div class="hint" style="text-align:center;padding:18px 0">' +
        '问点什么。它会并行查自有知识库、真实语料和 Lolibrary 档案，' +
        '需要时再补查小红书、微博和网页。</div>';
      return;
    }
    state.messages.forEach((message) => host.appendChild(messageNode(message)));
    host.scrollTop = host.scrollHeight;
  }

  function historyPayload() {
    return state.messages
      .filter((message) => !message.error && message.content)
      .slice(-MAX_HISTORY)
      .map((message) => ({ role: message.role, content: String(message.content).slice(0, 500) }));
  }

  async function ask(question, ui) {
    if (state.busy) return;
    state.busy = true;
    ui.input.disabled = true;
    ui.send.disabled = true;

    const userMessage = { role: 'user', content: question };
    const history = historyPayload();      // 在追加本轮问题之前取，否则问题会被发两遍
    state.messages.push(userMessage);

    const answer = { role: 'assistant', content: '', steps: [], evidence: [] };
    state.messages.push(answer);
    paintMessages(ui.list);

    const bubble = ui.list.lastElementChild.querySelector('.bubble');
    let steps = [];
    const started = performance.now();

    try {
      const response = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json; charset=utf-8' },
        body: JSON.stringify({ message: question, history: history }),
      });

      if (!response.ok) {
        let detail = response.status + ' ' + response.statusText;
        try { const data = await response.json(); if (data.detail) detail = data.detail; } catch (_) { /* 非 JSON */ }
        throw new Error(detail);
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
          if (!line) continue;
          let event;
          try { event = JSON.parse(line.slice(6)); } catch (_) { continue; }

          if (event.type === 'step') {
            steps.push(event);
            if ((event.kind === 'recall' || event.kind === 'skip') && !answer.content) {
              bubble.innerHTML = '<span class="thinking">正在召回…</span>';
            }
          } else if (event.type === 'delta') {
            answer.content += event.text;
            bubble.innerHTML = formatText(answer.content);
            ui.list.scrollTop = ui.list.scrollHeight;
          } else if (event.type === 'replace') {
            answer.content = event.text;
            bubble.innerHTML = formatText(answer.content);
          } else if (event.type === 'final') {
            answer.content = event.answer;
            answer.steps = event.steps || steps;
            answer.evidence = event.evidence || [];
            answer.refused = !!event.refused;
          } else if (event.type === 'error') {
            throw new Error(event.detail || '流式失败');
          }
        }
      }

      answer.ms = Math.round(performance.now() - started);
      paintMessages(ui.list);
    } catch (error) {
      answer.error = true;
      answer.content = '出错了：' + error.message;
      paintMessages(ui.list);
    } finally {
      state.busy = false;
      ui.input.disabled = false;
      ui.send.disabled = false;
      ui.input.focus();
    }
  }

  async function render() {
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
        '<div class="notice warn">' + esc(status.hint || '需要配置模型') + '</div></div>';
      return;
    }

    root.innerHTML =
      '<div class="card chat-card">' +
        '<div class="chat-head">' +
          '<span class="chat-model">' + esc(status.model) + '</span>' +
          '<button class="ghost sm" id="chat-clear">清空对话</button>' +
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
      '</div>';

    const ui = {
      list: document.getElementById('chat-list'),
      input: document.getElementById('chat-text'),
      send: document.getElementById('chat-send'),
    };

    const send = () => {
      const question = ui.input.value.trim();
      if (!question) { toast('先写一个问题'); return; }
      ui.input.value = '';
      ask(question, ui);
    };

    ui.send.addEventListener('click', send);
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
    document.getElementById('chat-clear').addEventListener('click', () => {
      state.messages = [];
      paintMessages(ui.list);
    });

    paintMessages(ui.list);
    ui.input.focus();
  }

  render();
})();
