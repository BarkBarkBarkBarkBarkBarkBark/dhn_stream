/* DHN Dashboard — WebSocket client + Chart.js + source selection + waveform preview */
'use strict';

// ── Constants ──────────────────────────────────────────────────────────────
const CH_COLORS = ['#58a6ff', '#3fb950', '#f78166', '#d2a8ff', '#ffa657', '#79c0ff', '#56d364', '#ff7b72'];

// ── State ──────────────────────────────────────────────────────────────────
let currentSource = 'sine_harmonics';
let previewData   = null;   // last fetched preview response
let previewChart  = null;
let spectrumChart = null;
let ws            = null;

// ── Oscilloscope state ────────────────────────────────────────────────────────
const SCOPE_PUSH   = 256;   // display samples per server push
const SCOPE_N      = 512;   // scrolling display — 2× push so you always see 100 ms of history
const SCOPE_OFFSET = 2.5;   // vertical separation between channels
let scopeSentChart = null;
let scopeRecvChart = null;
let scopeSentNch   = 0;     // channels currently initialised for each scope
let scopeRecvNch   = 0;

// Pending wave data: set by WS handler, consumed by RAF loop
let pendingSentWave = null;
let pendingRecvWave = null;
let scopeDirty      = false;

// Spectrum EMA smoothing state
let spectrumSmoothed = null;   // last smoothed dB array
let spectrumChSel    = -1;     // which channel the smoothed state belongs to
const EMA_ALPHA      = 0.1;    // blend factor — 0.1 ≈ 1-second settling at 10 Hz

// ── Helpers ────────────────────────────────────────────────────────────────
function log(msg) {
  const box = document.getElementById('log');
  const ts = new Date().toLocaleTimeString();
  box.innerHTML += `<div>[${ts}] ${msg}</div>`;
  box.scrollTop = box.scrollHeight;
}
function val(id) { return document.getElementById(id).value; }
function eid(id) { return document.getElementById(id); }

// ── Config collectors (per source) ─────────────────────────────────────────
function getConfig() {
  const base = {
    source:        currentSource,
    dest_host:     val('cfg-dest-host'),
    dest_port:     parseInt(val('cfg-dest-port'), 10),
    bind_host:     val('cfg-bind-host'),
    receiver_port: parseInt(val('cfg-recv-port'), 10),
    target_peak:   parseInt(val('cfg-peak'), 10),
    send_buffer:   parseInt(val('cfg-buf'), 10),
  };
  if (currentSource === 'sine_harmonics') {
    Object.assign(base, {
      channels:          parseInt(val('cfg-channels'), 10),
      sample_rate_hz:    parseInt(val('cfg-sr'), 10),
      fundamental_hz:    parseFloat(val('cfg-fundamental')),
      frames_per_packet: parseInt(val('cfg-fpp'), 10),
    });
  } else if (currentSource === 'spikeinterface') {
    Object.assign(base, {
      channels:          parseInt(val('cfg-si-channels'), 10),
      sample_rate_hz:    parseInt(val('cfg-si-sr'), 10),
      units:             parseInt(val('cfg-si-units'), 10),
      seed:              parseInt(val('cfg-si-seed'), 10),
      duration_seconds:  parseInt(val('cfg-si-dur'), 10),
      frames_per_packet: parseInt(val('cfg-si-fpp'), 10),
    });
  } else if (currentSource === 'file_replay') {
    Object.assign(base, {
      file_path:         val('cfg-file-path'),
      channels:          parseInt(val('cfg-file-channels'), 10),
      sample_rate_hz:    parseInt(val('cfg-file-sr'), 10),
      frames_per_packet: 1,
    });
  } else if (currentSource === 'udp_passthrough') {
    Object.assign(base, {
      bind_host:   val('cfg-pt-bind'),
      recv_port:   parseInt(val('cfg-pt-recv-port'), 10),
      dest_host:   val('cfg-pt-dest'),
      dest_port:   parseInt(val('cfg-pt-dest-port'), 10),
    });
  }
  return base;
}

// ── Harmonic legend ─────────────────────────────────────────────────────────
function buildHarmonicLegend(f) {
  const el = eid('harmonic-legend');
  const entries = [
    [`Ch 0`, `${f} Hz (f₁ only)`],
    [`Ch 1`, `${f} + ${f*2} Hz (f₁ + f₂)`],
    [`Ch 2`, `${f}, ${f*2}, ${f*3} Hz (f₁–f₃)`],
    [`Ch 3`, `${f}, ${f*2}, ${f*3}, ${f*4} Hz (f₁–f₄)`],
  ];
  el.innerHTML = entries.map(([ch, harm], i) =>
    `<div class="channel-row d-flex align-items-center py-1 px-2 mb-1">
       <span style="color:${CH_COLORS[i]};font-weight:600;min-width:40px">${ch}</span>
       <span class="ch-harmonics ms-2">${harm}</span>
     </div>`
  ).join('');
}

eid('cfg-fundamental').addEventListener('input', () =>
  buildHarmonicLegend(parseFloat(val('cfg-fundamental')) || 440));
buildHarmonicLegend(440);

// ── Source selection ────────────────────────────────────────────────────────
const ALL_SOURCES = ['sine_harmonics', 'spikeinterface', 'file_replay', 'udp_passthrough'];
const NO_PREVIEW  = ['udp_passthrough'];

function switchSource(src) {
  currentSource = src;
  document.querySelectorAll('#source-cards .source-card').forEach(c => {
    c.classList.toggle('selected', c.dataset.source === src);
  });
  ALL_SOURCES.forEach(s => {
    const p = eid('params-' + s);
    if (p) p.style.display = (s === src) ? '' : 'none';
  });
  const showPreview = !NO_PREVIEW.includes(src);
  eid('preview-button-row').style.display = showPreview ? '' : 'none';
}

document.querySelectorAll('#source-cards .source-card').forEach(c => {
  c.addEventListener('click', () => switchSource(c.dataset.source));
});
switchSource('sine_harmonics');

// ── Waveform preview ─────────────────────────────────────────────────────────
const CH_COLORS_PREVIEW = [...CH_COLORS];

function buildPreviewChannelSelect(nCh, labels) {
  const sel = eid('preview-channel-select');
  sel.innerHTML = '';
  // "All" option
  const all = document.createElement('option');
  all.value = '-1'; all.textContent = 'All channels';
  sel.appendChild(all);
  for (let i = 0; i < nCh; i++) {
    const opt = document.createElement('option');
    opt.value = i;
    opt.textContent = labels ? labels[i] : `Ch ${i}`;
    sel.appendChild(opt);
  }
  sel.onchange = () => renderPreviewChart();
}

function renderPreviewChart() {
  if (!previewData) return;
  const { time_s, channels, channel_labels } = previewData;
  const selVal = parseInt(eid('preview-channel-select').value, 10);
  const show   = selVal === -1 ? channels.map((_, i) => i) : [selVal];

  const datasets = show.map(i => ({
    label:           channel_labels ? channel_labels[i] : `Ch ${i}`,
    data:            time_s.map((t, j) => ({ x: t, y: channels[i][j] })),
    borderColor:     CH_COLORS_PREVIEW[i % CH_COLORS_PREVIEW.length],
    borderWidth:     1.2,
    pointRadius:     0,
    tension:         0,
  }));

  if (previewChart) { previewChart.destroy(); previewChart = null; }
  const ctx = eid('preview-chart').getContext('2d');
  previewChart = new Chart(ctx, {
    type: 'line',
    data: { datasets },
    options: {
      animation: false,
      parsing:   false,
      plugins: { legend: { labels: { color: '#8b949e', boxWidth: 12, font: { size: 11 } } } },
      scales: {
        x: { type: 'linear', title: { display: true, text: 'time (s)', color: '#8b949e' },
             ticks: { color: '#8b949e' }, grid: { color: '#21262d' } },
        y: { title: { display: true, text: 'norm. amp', color: '#8b949e' },
             ticks: { color: '#8b949e' }, grid: { color: '#21262d' }, min: -1.1, max: 1.1 },
      },
    },
  });
}

function renderPreviewProps(props) {
  const el = eid('preview-props-modal');
  if (!el) return;
  const badges = Object.entries(props).map(([k, v]) =>
    `<span class="prop-badge">${k}: <b>${Array.isArray(v) ? v.join(', ') : v}</b></span>`
  );
  el.innerHTML = badges.join('');
}

// ── Preview modal ─────────────────────────────────────────────────────────────
let _previewModal = null;
function getPreviewModal() {
  if (!_previewModal) _previewModal = new bootstrap.Modal(eid('previewModal'));
  return _previewModal;
}

// Render is deferred until modal is fully visible (so canvas has real dimensions)
eid('previewModal').addEventListener('shown.bs.modal', () => {
  if (previewData) {
    renderPreviewChart();
    if (previewData.properties) renderPreviewProps(previewData.properties);
  }
});

eid('btn-preview').addEventListener('click', async () => {
  const cfg = getConfig();
  log('Fetching waveform preview…');
  try {
    const res = await fetch('/api/preview/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source: currentSource, config: cfg }),
    });
    const data = await res.json();
    if (!res.ok) { log(`Preview error: ${data.error}`); return; }
    previewData = data;
    buildPreviewChannelSelect(data.channels.length, data.channel_labels);
    log(`Preview loaded: ${data.channels.length} ch × ${data.time_s.length} samples @ ${data.sample_rate_hz} Hz`);
    getPreviewModal().show();   // render fires on shown.bs.modal event
  } catch (e) {
    log(`Preview failed: ${e}`);
  }
});

// ── File upload ─────────────────────────────────────────────────────────────
eid('btn-upload').addEventListener('click', async () => {
  const input  = eid('file-upload-input');
  const status = eid('upload-status');
  if (!input.files.length) { status.textContent = 'No file selected.'; return; }
  const fd = new FormData();
  fd.append('file', input.files[0]);
  status.textContent = 'Uploading…';
  try {
    const res  = await fetch('/api/upload/', { method: 'POST', body: fd });
    const data = await res.json();
    if (!res.ok) { status.textContent = `Error: ${data.error}`; return; }
    status.textContent = `✓ Uploaded: ${data.filename} (${(data.size_bytes / 1024).toFixed(1)} KB)`;
    eid('cfg-file-path').value = data.path;
    log(`File uploaded → ${data.path}`);
    refreshFileBrowser();
  } catch (e) {
    status.textContent = `Upload failed: ${e}`;
  }
});

// ── File browser ─────────────────────────────────────────────────────────────
async function refreshFileBrowser() {
  const browser = eid('file-browser');
  try {
    const res  = await fetch('/api/files/?dir=uploads');
    const data = await res.json();
    if (!res.ok) { browser.innerHTML = `<div style="color:#f85149">${data.error}</div>`; return; }
    if (!data.entries.length) {
      browser.innerHTML = `<div style="font-size:0.78rem;color:#8b949e;padding:4px">No files in uploads/</div>`;
      return;
    }
    const selected = val('cfg-file-path');
    browser.innerHTML = data.entries
      .filter(e => !e.is_dir)
      .map(e =>
        `<div class="file-entry${e.path === selected ? ' selected-file' : ''}" data-path="${e.path}">
           ${e.name} <span style="color:#6e7681">(${(e.size_bytes/1024).toFixed(1)} KB)</span>
         </div>`
      ).join('');
    browser.querySelectorAll('.file-entry').forEach(el => {
      el.addEventListener('click', () => {
        eid('cfg-file-path').value = el.dataset.path;
        browser.querySelectorAll('.file-entry').forEach(x => x.classList.remove('selected-file'));
        el.classList.add('selected-file');
        log(`Selected file: ${el.dataset.path}`);
      });
    });
  } catch (e) {
    browser.innerHTML = `<div style="color:#f85149">Failed to load files</div>`;
  }
}

eid('btn-refresh-files').addEventListener('click', refreshFileBrowser);

// ── Spectrum chart ───────────────────────────────────────────────────────────
function initSpectrumChart() {
  const ctx = eid('spectrum-chart').getContext('2d');
  spectrumChart = new Chart(ctx, {
    type: 'line',
    data: {
      datasets: [{
        label: 'Power (dB)',
        data: [],
        borderColor: '#58a6ff',
        borderWidth: 1.5,
        pointRadius: 0,
        fill: true,
        backgroundColor: 'rgba(88,166,255,0.08)',
      }],
    },
    options: {
      animation: false,
      parsing: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { type: 'linear', title: { display: true, text: 'Hz', color: '#8b949e' },
             ticks: { color: '#8b949e' }, grid: { color: '#21262d' } },
        y: { title: { display: true, text: 'dB', color: '#8b949e' },
             ticks: { color: '#8b949e' }, grid: { color: '#21262d' } },
      },
    },
  });
}

function refreshSpectrumChart(freqs, magnitudes, fundamental, chSel) {
  if (!spectrumChart) return;
  // EMA smoothing: reset when channel changes or on first call
  if (chSel !== spectrumChSel || !spectrumSmoothed || spectrumSmoothed.length !== magnitudes.length) {
    spectrumSmoothed = magnitudes.slice();
    spectrumChSel    = chSel;
  } else {
    for (let i = 0; i < spectrumSmoothed.length; i++)
      spectrumSmoothed[i] = EMA_ALPHA * magnitudes[i] + (1 - EMA_ALPHA) * spectrumSmoothed[i];
  }
  const maxHz = Math.min(fundamental * 6, freqs[freqs.length - 1]);
  const pts = [];
  for (let i = 0; i < freqs.length; i++) {
    if (freqs[i] > maxHz) break;
    pts.push({ x: freqs[i], y: spectrumSmoothed[i] });
  }
  spectrumChart.data.datasets[0].data = pts;
  spectrumChart.options.scales.x.max = maxHz;
  spectrumChart.update('none');
}

// ── Oscilloscope charts ──────────────────────────────────────────────────────

function makeScope(canvasId, nCh) {
  const canvas = eid(canvasId);
  if (canvas._scopeChart) { canvas._scopeChart.destroy(); canvas._scopeChart = null; }
  const yMax   = (nCh - 1) * SCOPE_OFFSET + 1.3;
  const labels = Array.from({length: SCOPE_N}, (_, i) => i);
  const datasets = Array.from({length: nCh}, (_, i) => ({
    label:       `Ch ${i}`,
    data:        new Array(SCOPE_N).fill(i * SCOPE_OFFSET),
    borderColor: CH_COLORS[i % CH_COLORS.length],
    borderWidth: 1,
    pointRadius: 0,
    tension:     0,
    fill:        false,
  }));
  const chart = new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: { labels, datasets },
    options: {
      animation:           false,
      responsive:          true,
      maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#8b949e', boxWidth: 10, font: { size: 10 } } } },
      scales: {
        x: { display: false },
        y: {
          min: -1.3, max: yMax,
          ticks: {
            color: '#8b949e',
            maxTicksLimit: nCh + 2,
            callback: v => {
              const ch = Math.round(v / SCOPE_OFFSET);
              return (Math.abs(v - ch * SCOPE_OFFSET) < 0.08 && ch >= 0 && ch < nCh) ? `Ch${ch}` : '';
            },
          },
          grid: { color: '#21262d' },
        },
      },
    },
  });
  canvas._scopeChart = chart;
  return chart;
}

function initSentScope(nCh) {
  scopeSentNch   = nCh;
  scopeSentChart = makeScope('scope-sent', nCh);
}
function initRecvScope(nCh) {
  scopeRecvNch   = nCh;
  scopeRecvChart = makeScope('scope-recv', nCh);
}

// Write incoming wave into the scope's scrolling data arrays
function applyScopeWave(chart, wave) {
  if (!chart) return;
  const datasets = chart.data.datasets;
  const nCh = Math.min(wave.length, datasets.length);
  for (let ch = 0; ch < nCh; ch++) {
    const src    = wave[ch];
    const dst    = datasets[ch].data;   // regular Array — has .copyWithin
    const n      = Math.min(src.length, SCOPE_N);
    const offset = ch * SCOPE_OFFSET;
    dst.copyWithin(0, n);               // scroll left by n samples
    for (let j = 0; j < n; j++) dst[SCOPE_N - n + j] = src[j] + offset;
  }
  chart.update('none');
}

function heartbeatDot(id) {
  const el = eid(id);
  if (!el) return;
  el.className = 'scope-dot live';
}

// ── requestAnimationFrame render loop (60 fps cap, only draws when dirty) ─────
function rafLoop() {
  if (scopeDirty) {
    if (pendingSentWave) {
      applyScopeWave(scopeSentChart, pendingSentWave);
      pendingSentWave = null;
    }
    if (pendingRecvWave) {
      applyScopeWave(scopeRecvChart, pendingRecvWave);
      pendingRecvWave = null;
    }
    scopeDirty = false;
  }
  requestAnimationFrame(rafLoop);
}

function renderLatency(lat, pktsTotal) {
  if (!lat) return;
  eid('lat-mean').textContent  = lat.mean_ms   != null ? lat.mean_ms.toFixed(1)   : '—';
  eid('lat-jitter').textContent = lat.jitter_ms != null ? lat.jitter_ms.toFixed(1) : '—';
  eid('lat-since').textContent  = lat.since_last_ms != null ? lat.since_last_ms.toFixed(0) : '—';
  eid('lat-pkts').textContent   = pktsTotal != null ? pktsTotal : '—';
}

// ── Channel verification panel ───────────────────────────────────────────────
function renderChannelPanel(data) {
  const panel = eid('channel-verify-panel');
  const rows = (data.channel_spectra || []).map((_, i) => {
    const matched = data.channel_matches ? data.channel_matches[i] : null;
    const cls     = matched === null ? 'match-unk' : matched ? 'match-ok' : 'match-fail';
    const label   = matched === null ? '—' : matched ? '✓ PASS' : '✗ FAIL';
    const exp     = data.expected_harmonics ? data.expected_harmonics[i] : [];
    return `<div class="channel-row d-flex align-items-center">
      <span class="ch-label me-3" style="color:${CH_COLORS[i % CH_COLORS.length]}">Ch ${i}</span>
      <span class="ch-harmonics flex-grow-1">${exp.map(h => Math.round(h) + ' Hz').join(' + ') || '—'}</span>
      <span class="ch-match ${cls}">${label}</span>
    </div>`;
  });
  panel.innerHTML = rows.join('') || '<div class="text-muted">No data yet</div>';
}

// ── WebSocket ────────────────────────────────────────────────────────────────
function connectWS() {
  ws = new WebSocket(`ws://${location.host}/ws/dashboard/`);
  const dot    = eid('status-dot');
  const wsText = eid('ws-status');

  ws.onopen = () => {
    dot.className = 'connected'; wsText.textContent = 'Connected';
    log('WebSocket connected');
  };
  ws.onclose = () => {
    dot.className = 'error'; wsText.textContent = 'Disconnected — reconnecting in 3 s…';
    log('WebSocket closed, reconnecting…');
    setTimeout(connectWS, 3000);
  };
  ws.onerror = e => { log(`WS error: ${e}`); };

  ws.onmessage = ev => {
    const msg = JSON.parse(ev.data);

    if (msg.ack === 'pong') { log('Pong received'); return; }

    if (msg.kind === 'sender_stats') {
      const d = msg.data || {};
      if (d.running === false) {
        eid('sender-badge').className = 'badge bg-secondary';
        eid('sender-badge').textContent = 'Sender: stopped';
        eid('scope-sent-dot').className = 'scope-dot';
        return;
      }
      // Field names match what sine_sender.py actually sends
      eid('stat-pkt-rate').textContent   = d.packet_rate     != null ? d.packet_rate.toFixed(0)    : '—';
      eid('stat-throughput').textContent = d.throughput_mbps != null ? d.throughput_mbps.toFixed(3) : '—';
      eid('stat-underruns').textContent  = d.underruns       != null ? d.underruns                 : '—';
      eid('stat-elapsed').textContent    = d.elapsed         != null ? d.elapsed.toFixed(1)         : '—';
      eid('sender-badge').className = 'badge bg-success';
      eid('sender-badge').textContent = 'Sender: running';
      if (d.wave) {
        const nCh = d.wave.length;
        if (!scopeSentChart || scopeSentNch !== nCh) initSentScope(nCh);
        pendingSentWave = d.wave;
        scopeDirty = true;
        heartbeatDot('scope-sent-dot');
      }
      return;
    }

    if (msg.kind === 'spectrum') {
      const d = msg.data || {};
      if (d.running === false) {
        eid('receiver-badge').className = 'badge bg-secondary';
        eid('receiver-badge').textContent = 'Receiver: stopped';
        eid('scope-recv-dot').className = 'scope-dot';
        return;
      }
      eid('receiver-badge').className = 'badge bg-success';
      eid('receiver-badge').textContent = 'Receiver: running';
      // FFT fields only present in the 1 Hz push
      if (d.freqs && d.channel_spectra) {
        const chSel    = parseInt(val('chart-channel-select'), 10);
        const chSpectra = d.channel_spectra || [];
        if (chSpectra[chSel]) {
          const fundamental = getConfig().fundamental_hz || 440;
          refreshSpectrumChart(d.freqs, chSpectra[chSel], fundamental, chSel);
        }
        renderChannelPanel(d);
        // Rebuild channel selector to match actual channel count
        const nCh = d.channel_spectra.length;
        const sel = eid('chart-channel-select');
        if (sel.options.length !== nCh) {
          sel.innerHTML = Array.from({length: nCh}, (_, i) =>
            `<option value="${i}"${i===0?' selected':''}>Ch ${i}</option>`
          ).join('');
        }
      }
      if (d.wave) {
        const nCh = d.wave.length;
        if (!scopeRecvChart || scopeRecvNch !== nCh) initRecvScope(nCh);
        pendingRecvWave = d.wave;
        scopeDirty = true;
        heartbeatDot('scope-recv-dot');
      }
      if (d.latency) renderLatency(d.latency, d.packets_total);
      return;
    }

    if (msg.kind === 'stopped') {
      if (msg.who === 'sender') {
        eid('sender-badge').className = 'badge bg-secondary';
        eid('sender-badge').textContent = 'Sender: stopped';
        eid('btn-start-send').disabled = false;
        eid('btn-stop-send').disabled  = true;
      } else if (msg.who === 'receiver') {
        eid('receiver-badge').className = 'badge bg-secondary';
        eid('receiver-badge').textContent = 'Receiver: stopped';
        eid('btn-start-recv').disabled = false;
        eid('btn-stop-recv').disabled  = true;
      }
    }
  };
}

function wsSend(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(obj));
  } else {
    log('WS not connected');
  }
}

// ── Button handlers ──────────────────────────────────────────────────────────
eid('btn-start-recv').addEventListener('click', () => {
  const cfg = getConfig();
  wsSend({ cmd: 'start_receiver', config: cfg });
  log('Starting receiver…');
  eid('btn-start-recv').disabled = true;
  eid('btn-stop-recv').disabled  = false;
});
eid('btn-stop-recv').addEventListener('click', () => {
  wsSend({ cmd: 'stop_receiver' });
  log('Stopping receiver…');
  eid('btn-start-recv').disabled = false;
  eid('btn-stop-recv').disabled  = true;
});
eid('btn-start-send').addEventListener('click', () => {
  const cfg = getConfig();
  wsSend({ cmd: 'start_sender', config: cfg });
  log('Starting sender…');
  eid('btn-start-send').disabled = true;
  eid('btn-stop-send').disabled  = false;
});
eid('btn-stop-send').addEventListener('click', () => {
  wsSend({ cmd: 'stop_sender' });
  log('Stopping sender…');
  eid('btn-start-send').disabled = false;
  eid('btn-stop-send').disabled  = true;
});

// ── Channel count live sync: reinit scopes immediately when user changes input ─
eid('cfg-channels').addEventListener('input', () => {
  const n = Math.max(1, parseInt(val('cfg-channels'), 10) || 4);
  initSentScope(n);
  initRecvScope(n);
});

// ── Init ─────────────────────────────────────────────────────────────────────
initSpectrumChart();
initSentScope(4);
initRecvScope(4);
requestAnimationFrame(rafLoop);
connectWS();
