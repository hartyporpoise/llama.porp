/**
 * Porpulsion dashboard — page refresh, render, form bindings.
 * Depends on window.Porpulsion (api.js + app.js). Uses /api for all requests.
 */
(function () {
  'use strict';
  var P = window.Porpulsion;
  if (!P) return;

  var _esc = P.esc;
  var statusBadge = P.statusBadge;
  var timeAgo = P.timeAgo;
  var toast = P.toast;
  var API_BASE = P.API_BASE;

  function el(id) { return document.getElementById(id); }

  function renderOverviewPeers(peers) {
    var body = el('overview-peers-body');
    var empty = el('overview-peers-empty');
    var badge = el('peers-count-badge');
    if (!body) return;
    var connected = peers.filter(function (p) { return !p.status || p.status === 'connected'; });
    if (badge) badge.textContent = connected.length;
    if (!connected.length) { body.innerHTML = ''; if (empty) empty.style.display = ''; return; }
    if (empty) empty.style.display = 'none';
    body.innerHTML = connected.map(function (p) {
      var wsConn = p.channel === 'connected';
      var encrypted = (p.url || '').indexOf('https://') === 0;
      var chanBadge = wsConn
        ? (encrypted ? '<span class="badge badge-mtls"><span class="badge-dot"></span>live</span>' : '<span class="badge badge-warn"><span class="badge-dot"></span>live</span>')
        : '<span class="badge badge-failed">offline</span>';
      return '<tr><td><strong>' + _esc(p.name) + '</strong></td><td class="mono">' + _esc(p.url || '') + '</td><td>' + chanBadge + '</td><td class="time-ago">' + timeAgo(p.connected_at) + '</td></tr>';
    }).join('');
  }

  function renderAllPeers(peers) {
    var body = el('all-peers-body');
    var empty = el('all-peers-empty');
    var countEl = el('all-peers-count');
    if (!body) return;
    if (countEl) countEl.textContent = peers.length;
    if (!peers.length) { body.innerHTML = ''; if (empty) empty.style.display = ''; return; }
    if (empty) empty.style.display = 'none';
    body.innerHTML = peers.map(function (p) {
      var status = p.status || 'connected';
      var statusCls = status === 'connected' ? 'badge-mtls' : status === 'connecting' ? 'badge-connecting' : status === 'awaiting_confirmation' ? 'badge-handshake' : 'badge-failed';
      var statusHtml = '<span class="badge ' + statusCls + '">' + (status === 'connected' ? '<span class="badge-dot"></span>' : '') + status + '</span>';
      var chanHtml = (p.channel === 'connected') ? '<span class="badge badge-mtls"><span class="badge-dot"></span>live</span>' : '<span class="badge badge-pending">—</span>';
      var actions = '';
      if (status === 'connecting') actions = '<button class="btn-sm peer-cancel-btn">Cancel</button>';
      else if (status === 'failed' || status === 'awaiting_confirmation') actions = '<button class="btn-sm peer-retry-btn">Retry</button>';
      else actions = '<button class="btn-sm btn-danger peer-remove-btn">Remove</button>';
      return '<tr data-peer-url="' + _esc(p.url) + '" data-peer-name="' + _esc(p.name) + '">' +
        '<td><strong>' + _esc(p.name) + '</strong></td>' +
        '<td class="mono">' + _esc(p.url || '') + '</td>' +
        '<td>' + statusHtml + '</td>' +
        '<td>' + chanHtml + '</td>' +
        '<td class="time-ago">' + timeAgo(p.connected_at) + '</td>' +
        '<td>' + actions + '</td></tr>';
    }).join('');
  }

  function renderInbound(list) {
    var banner = el('inbound-banner');
    var listEl = el('inbound-list');
    var badge = el('nav-inbound-badge');
    if (!banner) return;
    if (!list.length) {
      banner.classList.remove('visible');
      if (badge) { badge.style.display = 'none'; }
      if (listEl) listEl.innerHTML = '';
      return;
    }
    banner.classList.add('visible');
    if (badge) { badge.style.display = ''; badge.textContent = list.length; }
    if (listEl) {
      listEl.innerHTML = list.map(function (r) {
        var id = r.id;
        var name = (r.name || '').replace(/'/g, "\\'");
        return '<div class="inbound-item">' +
          '<div class="inbound-item-info"><div class="inbound-item-name">' + _esc(r.name) + '</div><div class="inbound-item-url">' + _esc(r.url || '') + '</div></div>' +
          '<div class="inbound-item-time">' + timeAgo(r.since) + '</div>' +
          '<div class="btn-row">' +
          '<button type="button" class="btn-sm btn-success" data-accept-inbound="' + _esc(id) + '" data-inbound-name="' + _esc(r.name || '') + '">Accept</button>' +
          '<button type="button" class="btn-sm btn-danger" data-reject-inbound="' + _esc(id) + '">Reject</button>' +
          '</div></div>';
      }).join('');
    }
  }

  function renderRecentApps(submitted, executing) {
    var body = el('recent-apps-body');
    var empty = el('recent-apps-empty');
    var countEl = el('recent-apps-count');
    if (!body) return;
    var all = submitted.map(function (a) { return Object.assign({}, a, { _type: 'submitted' }); }).concat(executing.map(function (a) { return Object.assign({}, a, { _type: 'executing' }); }));
    all.sort(function (a, b) { return new Date(b.updated_at) - new Date(a.updated_at); });
    all = all.slice(0, 8);
    if (countEl) countEl.textContent = all.length;
    if (!all.length) { body.innerHTML = ''; if (empty) empty.style.display = ''; return; }
    if (empty) empty.style.display = 'none';
    body.innerHTML = all.map(function (a) {
      var typeLabel = a._type === 'submitted' ? '<span class="badge badge-handshake" style="font-size:0.65rem;">outbound</span>' : '<span class="badge badge-inbound" style="font-size:0.65rem;">inbound</span>';
      return '<tr data-app-id="' + _esc(a.id) + '" data-app-name="' + _esc(a.name) + '">' +
        '<td><a href="#" class="app-open-link">' + _esc(a.name) + '</a></td>' +
        '<td>' + typeLabel + '</td><td>' + statusBadge(a.status) + '</td>' +
        '<td class="time-ago">' + timeAgo(a.updated_at) + '</td>' +
        '<td><span class="btn-row"><button type="button" class="btn-sm app-detail-btn">Detail</button><button type="button" class="btn-sm btn-danger app-delete-btn">Delete</button></span></td></tr>';
    }).join('');
  }

  function renderApps(list, bodyId, emptyId, countId, showSource) {
    var body = el(bodyId);
    if (!body) return;
    var empty = el(emptyId);
    var countEl = countId ? el(countId) : null;
    if (countEl) countEl.textContent = list.length;
    if (!list.length) { body.innerHTML = ''; if (empty) empty.style.display = ''; return; }
    if (empty) empty.style.display = 'none';
    var peerKey = showSource ? 'source_peer' : 'target_peer';
    body.innerHTML = list.map(function (a) {
      var isDead = a.status === 'Deleted' || a.status === 'Failed' || a.status === 'Timeout';
      var peerVal = a[peerKey] || '—';
      return '<tr' + (isDead ? ' style="opacity:0.55;"' : '') + ' data-app-id="' + _esc(a.id) + '" data-app-name="' + _esc(a.name) + '">' +
        '<td><a href="#" class="app-open-link">' + _esc(a.name) + '</a></td>' +
        '<td class="mono">' + _esc(a.id) + '</td><td>' + statusBadge(a.status) + '</td>' +
        '<td class="text-muted text-sm">' + _esc(peerVal) + '</td>' +
        '<td class="time-ago">' + timeAgo(a.updated_at) + '</td>' +
        '<td><span class="btn-row"><button type="button" class="btn-sm app-detail-btn">Detail</button><button type="button" class="btn-sm btn-danger app-delete-btn">Delete</button></span></td></tr>';
    }).join('');
  }

  function renderProxyApps(submitted) {
    var listEl = el('proxy-apps-list');
    if (!listEl) return;
    var active = submitted.filter(function (a) { return a.status !== 'Deleted' && a.status !== 'Failed' && a.status !== 'Timeout'; });
    if (!active.length) {
      listEl.innerHTML = '<div class="empty-state" style="padding:1.5rem 0.5rem;"><div class="empty-icon">⇒</div>No submitted apps yet</div>';
      return;
    }
    listEl.innerHTML = active.map(function (a) {
      var ports = (a.spec && Array.isArray(a.spec.ports) && a.spec.ports.length) ? a.spec.ports : [{ port: (a.spec && a.spec.port) || 80 }];
      var portLinks = ports.map(function (p) {
        var portNum = typeof p === 'object' ? (p.port || 80) : p;
        var portLabel = (p.name ? p.name + ' (' + portNum + ')' : portNum);
        var proxyUrl = window.location.origin + API_BASE + '/remoteapp/' + a.id + '/proxy/' + portNum;
        var id = 'proxy-url-' + a.id + '-' + portNum;
        return '<div style="display:flex;align-items:center;gap:0.5rem;padding:0.3rem 0;border-bottom:1px solid var(--border);">' +
          '<span class="mono" style="color:var(--muted);min-width:60px;font-size:0.75rem;">' + portLabel + '</span>' +
          '<span class="mono" id="' + id + '" style="flex:1;font-size:0.72rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--accent2);" title="' + _esc(proxyUrl) + '">' + _esc(proxyUrl) + '</span>' +
          '<button type="button" class="btn-sm" data-copy-el="' + id + '">Copy</button></div>';
      }).join('');
      return '<div style="padding:0.75rem 0;border-bottom:1px solid var(--border);">' +
        '<div style="display:flex;align-items:center;gap:0.5rem;margin-bottom:0.4rem;">' +
        '<strong style="font-size:0.845rem;">' + _esc(a.name) + '</strong>' + statusBadge(a.status) +
        '<span class="text-muted text-sm" style="margin-left:auto;">' + _esc(a.target_peer || '') + '</span></div>' + portLinks + '</div>';
    }).join('');
  }

  function renderApproval(list) {
    var banner = el('approval-banner');
    var listEl = el('approval-list');
    var badge = el('nav-approval-badge');
    if (!banner) return;
    if (!list.length) {
      banner.classList.remove('visible');
      if (badge) badge.style.display = 'none';
      if (listEl) listEl.innerHTML = '';
      return;
    }
    banner.classList.add('visible');
    if (badge) { badge.style.display = ''; badge.textContent = list.length; }
    if (!listEl) return;
    listEl.innerHTML = list.map(function (r) {
      var id = r.id;
      var spec = r.spec || {};
      var image = spec.image || '';
      var replicas = spec.replicas || 1;
      return '<div class="approval-item">' +
        '<div class="approval-item-header">' +
        '<div class="approval-item-info">' +
        '<div class="approval-item-name">' + _esc(r.name || id) + '</div>' +
        '<div class="approval-item-meta">from <strong>' + _esc(r.source_peer || '?') + '</strong>' + (image ? ' · <span class="mono">' + _esc(image) + '</span>' : '') + ' · ' + replicas + ' replica(s)</div></div>' +
        '<div class="approval-item-time">' + timeAgo(r.since) + '</div>' +
        '<div class="btn-row">' +
        '<button type="button" class="btn-sm btn-success" data-approve-app="' + _esc(id) + '" data-approve-name="' + _esc(r.name || id) + '">Approve</button>' +
        '<button type="button" class="btn-sm btn-danger" data-reject-app="' + _esc(id) + '">Reject</button></div></div>' +
        '<div class="approval-item-spec"><pre class="approval-spec-pre">' + _esc(JSON.stringify(spec, null, 2)) + '</pre></div></div>';
    }).join('');
  }

  function refresh() {
    Promise.all([
      P.getPeers(),
      P.getRemoteApps(),
      P.getInbound(),
      P.getPendingApproval().catch(function () { return []; })
    ]).then(function (results) {
      var peers = results[0];
      var apps = results[1];
      var inbound = results[2];
      var approval = results[3];
      var submitted = apps.submitted || [];
      var executing = apps.executing || [];
      var connected = peers.filter(function (p) { return !p.status || (p.status !== 'connecting' && p.status !== 'awaiting_confirmation' && p.status !== 'failed'); });

      var statPeers = el('stat-peers');
      if (statPeers) statPeers.textContent = connected.length;
      var statSubmitted = el('stat-submitted');
      if (statSubmitted) statSubmitted.textContent = submitted.length;
      var statExecuting = el('stat-executing');
      if (statExecuting) statExecuting.textContent = executing.length;

      var connecting = peers.filter(function (p) { return p.status === 'connecting'; }).length;
      var awaiting = peers.filter(function (p) { return p.status === 'awaiting_confirmation'; }).length;
      var sub = [];
      if (connecting) sub.push(connecting + ' connecting');
      if (awaiting) sub.push(awaiting + ' awaiting');
      var statSub = el('stat-peers-sub');
      if (statSub) statSub.textContent = sub.length ? sub.join(', ') : (connected.length ? 'all connected' : 'no peers');

      var peerLabel = el('peer-count-label');
      if (peerLabel) peerLabel.textContent = connected.length + ' peer' + (connected.length !== 1 ? 's' : '') + (connecting ? ', ' + connecting + ' connecting' : '') + (awaiting ? ', ' + awaiting + ' waiting' : '');

      renderOverviewPeers(peers);
      renderAllPeers(peers);
      renderInbound(inbound);
      renderApproval(approval);
      renderRecentApps(submitted, executing);
      renderApps(submitted, 'submitted-body', 'submitted-empty', 'submitted-count', false);
      renderApps(executing, 'executing-body', 'executing-empty', 'executing-count', true);
      renderProxyApps(submitted);

      var healthDot = el('health-dot');
      if (healthDot) healthDot.className = 'health-dot';
      var lastRefresh = el('last-refresh');
      if (lastRefresh) lastRefresh.textContent = new Date().toLocaleTimeString();
    }).catch(function () {
      var healthDot = el('health-dot');
      if (healthDot) healthDot.className = 'health-dot red';
    });
  }

  function loadToken() {
    P.getToken().then(function (d) {
      var url = d.self_url || '(not set)';
      var token = d.invite_token || '';
      var fp = d.cert_fingerprint || '';
      P.setSecret('token-key', token);
      P.setSecret('token-fp-peers', fp);
      var tokenUrl = el('token-url');
      if (tokenUrl) tokenUrl.textContent = url;
      P.setSecret('settings-token-key', token);
      P.setSecret('token-fp', fp);
      var settingsUrl = el('settings-token-url');
      if (settingsUrl) settingsUrl.textContent = url;
      P.setSecret('token-pem', d.ca_pem || '');
      var aboutUrl = el('about-url');
      if (aboutUrl) aboutUrl.textContent = url;
    }).catch(function () {});
  }

  function setSegVal(ctrlId, activeBtn) {
    var ctrl = el(ctrlId);
    if (!ctrl) return;
    var val = activeBtn && activeBtn.dataset && activeBtn.dataset.val;
    var btns = ctrl.querySelectorAll('button[data-val]');
    for (var i = 0; i < btns.length; i++) {
      btns[i].classList.toggle('active', btns[i] === activeBtn);
    }
  }
  function saveSetting(key, value) {
    var payload = {};
    payload[key] = value;
    P.updateSettings(payload).then(function () {
      toast('Saved', 'ok');
    }).catch(function (err) { toast(err.message, 'error'); });
  }
  function saveInboundTunnels(enabled) {
    saveSetting('allow_inbound_tunnels', enabled);
  }

  function loadSettings() {
    var logLevelCtrl = el('setting-log-level');
    var inboundApps = el('setting-inbound-apps');
    var requireApproval = el('setting-require-approval');
    var inboundTunnels = el('setting-inbound-tunnels');
    if (!logLevelCtrl && !inboundApps) return;
    P.getSettings().then(function (s) {
      var level = (s.log_level || 'INFO').toUpperCase();
      if (logLevelCtrl) {
        var btns = logLevelCtrl.querySelectorAll('button[data-val]');
        for (var i = 0; i < btns.length; i++) {
          btns[i].classList.toggle('active', (btns[i].dataset.val || '') === level);
        }
      }
      if (inboundApps) inboundApps.checked = !!s.allow_inbound_remoteapps;
      if (requireApproval) requireApproval.checked = !!s.require_remoteapp_approval;
      if (inboundTunnels) inboundTunnels.checked = !!s.allow_inbound_tunnels;
    }).catch(function () {});
  }

  function loadLogs() {
    var workloadSelect = el('logs-workload');
    var content = el('logs-content');
    var tailSelect = el('logs-tail');
    var viewSelect = el('logs-view');
    var refreshBtn = el('logs-refresh');
    if (!workloadSelect || !content) return;
    function fetchAndShow() {
      var appId = workloadSelect.value;
      if (!appId) { content.textContent = 'Select a workload to view its pod logs.'; return; }
      var tail = (tailSelect && tailSelect.value) ? parseInt(tailSelect.value, 10) : 200;
      var order = (viewSelect && viewSelect.value) || 'pod';
      content.textContent = 'Loading…';
      P.getAppLogs(appId, tail, order).then(function (d) {
        var lines = (d && d.lines) ? d.lines : [];
        var text = lines.map(function (l) {
          return (l.ts ? l.ts + ' ' : '') + (l.pod ? '[' + l.pod + '] ' : '') + (l.message || '');
        }).join('\n');
        if (d && d.error && !lines.length) text = 'Error: ' + d.error + '\n' + text;
        content.textContent = text || '(no logs)';
      }).catch(function (err) {
        content.textContent = 'Error: ' + (err.message || 'Failed to load logs');
      });
    }
    function populateWorkloads() {
      P.getRemoteApps().then(function (apps) {
        var submitted = apps.submitted || [];
        var executing = apps.executing || [];
        var all = submitted.concat(executing);
        var cur = workloadSelect.value;
        workloadSelect.innerHTML = '<option value="">Select a workload…</option>';
        all.forEach(function (a) {
          var opt = document.createElement('option');
          opt.value = a.id;
          opt.textContent = (a.name || a.id) + ' (' + (a.status || '') + ')';
          workloadSelect.appendChild(opt);
        });
        if (cur) workloadSelect.value = cur;
      }).catch(function () {});
    }
    populateWorkloads();
    workloadSelect.addEventListener('change', fetchAndShow);
    if (tailSelect) tailSelect.addEventListener('change', fetchAndShow);
    if (viewSelect) viewSelect.addEventListener('change', fetchAndShow);
    if (refreshBtn) refreshBtn.addEventListener('click', function () { populateWorkloads(); fetchAndShow(); });
    var autoInterval = null;
    var cb = el('logs-auto-refresh');
    if (cb) {
      cb.addEventListener('change', function () {
        if (autoInterval) { clearInterval(autoInterval); autoInterval = null; }
        if (cb.checked) autoInterval = setInterval(fetchAndShow, 3000);
      });
    }
  }

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('button, a');
    if (!btn) return;
    if (btn.classList.contains('app-open-link') || btn.classList.contains('app-detail-btn')) {
      e.preventDefault();
      var row = btn.closest('tr[data-app-id]');
      if (row) openAppModal(row.dataset.appId);
    } else if (btn.classList.contains('app-delete-btn')) {
      e.preventDefault();
      var row = btn.closest('tr[data-app-id]');
      if (row) deleteApp(row.dataset.appId, row.dataset.appName);
    } else if (btn.classList.contains('peer-remove-btn')) {
      e.preventDefault();
      var row = btn.closest('tr[data-peer-url]');
      if (row) removePeer(row.dataset.peerName);
    } else if (btn.dataset.acceptInbound) {
      e.preventDefault();
      P.acceptInbound(btn.dataset.acceptInbound).then(function () { toast('Connected', 'ok'); refresh(); }).catch(function (err) { toast('Failed: ' + err.message, 'error'); refresh(); });
    } else if (btn.dataset.rejectInbound) {
      e.preventDefault();
      P.rejectInbound(btn.dataset.rejectInbound).then(function () { toast('Rejected', 'ok'); refresh(); }).catch(function () { refresh(); });
    } else if (btn.dataset.approveApp) {
      e.preventDefault();
      P.approveApp(btn.dataset.approveApp).then(function () { toast('Approved ' + (btn.dataset.approveName || ''), 'ok'); refresh(); }).catch(function (err) { toast(err.message, 'error'); refresh(); });
    } else if (btn.dataset.rejectApp) {
      e.preventDefault();
      P.rejectApp(btn.dataset.rejectApp).then(function () { toast('Rejected', 'ok'); refresh(); }).catch(function () { refresh(); });
    } else if (btn.dataset.copyEl) {
      e.preventDefault();
      P.copyText(btn.dataset.copyEl, btn);
    }
  });

  var deployForm = el('deploy-form');
  if (deployForm) {
    deployForm.addEventListener('submit', function (e) {
      e.preventDefault();
      var nameEl = el('app-name');
      var name = nameEl ? nameEl.value.trim() : '';
      var yamlEl = el('app-spec-yaml');
      var yaml = yamlEl ? yamlEl.value : '';
      if (!name) return;
      if (!yaml.trim()) { toast('Spec cannot be empty', 'error'); return; }
      var spec;
      try {
        spec = parseSimpleYaml(yaml);
      } catch (err) { toast('Invalid YAML: ' + err.message, 'error'); return; }
      if (!spec.image) { toast('Spec must include an "image" field', 'error'); return; }
      P.createRemoteApp({ name: name, spec: spec }).then(function () {
        toast('Deployed ' + name, 'ok');
        if (nameEl) nameEl.value = '';
        if (yamlEl) yamlEl.value = 'image: nginx:latest\nreplicas: 1\nports:\n  - port: 80\n    name: http';
        setTimeout(refresh, 500);
      }).catch(function (err) {
        if (err.message && err.message.indexOf('inbound') !== -1) toast('Remote agent has inbound workloads disabled — enable in peer Settings', 'warn');
        else toast('Error: ' + err.message, 'error');
      });
    });
  }

  document.body.addEventListener('htmx:afterOnLoad', function (ev) {
    if (ev.detail.target.id !== 'connect-peer-form') return;
    var xhr = ev.detail.xhr;
    var success = xhr && xhr.status >= 200 && xhr.status < 300;
    try {
      var d = JSON.parse(xhr.responseText || '{}');
      if (success) {
        toast(d.message || 'Peering initiated', 'ok');
        if (el('new-peer-url')) el('new-peer-url').value = '';
        if (el('new-peer-token')) el('new-peer-token').value = '';
        if (el('new-peer-fp')) el('new-peer-fp').value = '';
        setTimeout(refresh, 500);
      } else {
        toast(d.error || xhr.statusText || 'Failed', 'error');
      }
    } catch (e) {
      toast(success ? 'Connected' : (xhr.statusText || 'Error'), success ? 'ok' : 'error');
      if (success) setTimeout(refresh, 500);
    }
  });

  var _currentAppId = null;
  function openAppModal(appId) {
    _currentAppId = appId;
    var modal = el('app-modal');
    var title = el('app-modal-title');
    var body = el('app-modal-body');
    if (!modal || !body) return;
    if (title) title.textContent = 'App Detail';
    body.innerHTML = '<p class="text-muted text-sm">Loading…</p>';
    modal.classList.add('open');
    P.getAppDetail(appId).then(function (d) {
      var app = d.app || {};
      var spec = app.spec || {};
      var html = '<div class="detail-grid"><div class="detail-block"><h4>App Info</h4>' +
        '<div class="detail-row"><span class="label">ID</span><span class="mono">' + _esc(app.id) + '</span></div>' +
        '<div class="detail-row"><span class="label">Status</span>' + statusBadge(app.status) + '</div>' +
        '<div class="detail-row"><span class="label">Running on</span><span>' + _esc(app.target_peer || app.source_peer || '—') + '</span></div>' +
        '<div class="detail-row"><span class="label">Updated</span><span class="time-ago">' + timeAgo(app.updated_at) + '</span></div></div>' +
        '<div class="detail-block"><h4>Spec</h4>' +
        '<div class="detail-row"><span class="label">Image</span><span class="mono">' + _esc(spec.image || '—') + '</span></div>' +
        '<div class="detail-row"><span class="label">Replicas</span><span>' + (spec.replicas || 1) + '</span></div></div></div>';
      if ((spec.ports || []).length) {
        html += '<div class="detail-block" style="margin-top:0.75rem;"><h4>Proxy URLs</h4>';
        (spec.ports || []).forEach(function (p) {
          var portNum = p.port || 80;
          var proxyUrl = window.location.origin + P.API_BASE + '/remoteapp/' + app.id + '/proxy/' + portNum;
          html += '<div class="detail-row"><span class="label">' + portNum + (p.name ? ' (' + p.name + ')' : '') + '</span><span class="mono" style="font-size:0.72rem;word-break:break-all;">' + _esc(proxyUrl) + '</span></div>';
        });
        html += '</div>';
      }
      html += '<div class="detail-actions"><button type="button" class="btn-sm btn-danger app-modal-delete-btn">Delete</button></div>';
      body.innerHTML = html;
      var delBtn = body.querySelector('.app-modal-delete-btn');
      if (delBtn) delBtn.addEventListener('click', function () { deleteApp(app.id, app.name); });
    }).catch(function (err) {
      body.innerHTML = '<p style="color:var(--red)">Error: ' + _esc(err.message) + '</p>';
    });
  }
  function closeAppModal() {
    _currentAppId = null;
    var modal = el('app-modal');
    if (modal) modal.classList.remove('open');
  }
  function deleteApp(id, name) {
    P.deleteApp(id).then(function () {
      toast('Deleted ' + (name || id), 'ok');
      closeAppModal();
      refresh();
    }).catch(function (err) { toast('Error: ' + err.message, 'error'); refresh(); });
  }
  function removePeer(name) {
    P.removePeer(name).then(function () { toast('Removed ' + name, 'ok'); refresh(); }).catch(function (err) { toast(err.message, 'error'); refresh(); });
  }

  var appModal = el('app-modal');
  if (appModal) appModal.addEventListener('click', function (e) { if (e.target === this) closeAppModal(); });
  var appModalClose = el('app-modal-close');
  if (appModalClose) appModalClose.addEventListener('click', closeAppModal);

  (function bindSettings() {
    var logLevelCtrl = el('setting-log-level');
    if (logLevelCtrl) {
      logLevelCtrl.addEventListener('click', function (e) {
        var btn = e.target.closest('button[data-val]');
        if (!btn) return;
        setSegVal('setting-log-level', btn);
        saveSetting('log_level', btn.dataset.val);
      });
    }
    var inboundApps = el('setting-inbound-apps');
    if (inboundApps) inboundApps.addEventListener('change', function () { saveSetting('allow_inbound_remoteapps', inboundApps.checked); });
    var requireApproval = el('setting-require-approval');
    if (requireApproval) requireApproval.addEventListener('change', function () { saveSetting('require_remoteapp_approval', requireApproval.checked); });
    var inboundTunnels = el('setting-inbound-tunnels');
    if (inboundTunnels) inboundTunnels.addEventListener('change', function () { saveInboundTunnels(inboundTunnels.checked); });
  })();

  window.PorpulsionPages = {
    refresh: refresh,
    loadToken: loadToken,
    openAppModal: openAppModal,
    closeAppModal: closeAppModal,
    deleteApp: deleteApp,
    removePeer: removePeer,
    parseSimpleYaml: parseSimpleYaml
  };

  function parseSimpleYaml(text) {
    var lines = text.split('\n');
    function parseBlock(startIdx, minIndent) {
      var obj = {};
      var i = startIdx;
      while (i < lines.length) {
        var line = lines[i];
        var trimmed = line.trim();
        if (!trimmed || trimmed.charAt(0) === '#') { i++; continue; }
        var indent = line.search(/\S/);
        if (indent < minIndent) break;
        var idx = trimmed.indexOf(':');
        if (idx === -1) { i++; continue; }
        var key = trimmed.slice(0, idx).trim();
        var val = trimmed.slice(idx + 1).trim();
        if (val) {
          if (/^\d+$/.test(val)) val = parseInt(val, 10);
          else if (val === 'true') val = true;
          else if (val === 'false') val = false;
          obj[key] = val;
          i++;
        } else {
          i++;
          var j = i;
          while (j < lines.length && !lines[j].trim()) j++;
          if (j >= lines.length) { obj[key] = {}; continue; }
          var nextTrimmed = lines[j].trim();
          var nextIndent = lines[j].search(/\S/);
          if (nextIndent <= indent) { obj[key] = {}; continue; }
          if (nextTrimmed.charAt(0) === '-') {
            var listItems = [];
            while (i < lines.length) {
              var itemLine = lines[i];
              var itemTrimmed = itemLine.trim();
              if (!itemTrimmed || itemTrimmed.charAt(0) === '#') { i++; continue; }
              var itemIndent = itemLine.search(/\S/);
              if (itemIndent <= indent) break;
              if (itemTrimmed.charAt(0) === '-') {
                var itemVal = itemTrimmed.slice(1).trim();
                var itemObj = {};
                var baseIndent = itemIndent;
                i++;
                while (i < lines.length) {
                  var contLine = lines[i];
                  var contTrimmed = contLine.trim();
                  if (!contTrimmed) { i++; continue; }
                  var contIndent = contLine.search(/\S/);
                  if (contIndent <= baseIndent) break;
                  var pi = contTrimmed.indexOf(':');
                  if (pi !== -1) {
                    var pk = contTrimmed.slice(0, pi).trim();
                    var pv = contTrimmed.slice(pi + 1).trim();
                    if (/^\d+$/.test(pv)) pv = parseInt(pv, 10);
                    itemObj[pk] = pv;
                  }
                  i++;
                }
                listItems.push(itemObj);
              } else i++;
            }
            obj[key] = listItems;
          } else {
            var sub = parseBlock(i, nextIndent);
            obj[key] = sub.obj;
            i = sub.nextIdx;
          }
        }
      }
      return { obj: obj, nextIdx: i };
    }
    return parseBlock(0, 0).obj;
  }

  refresh();
  loadToken();
  loadSettings();
  loadLogs();
  setInterval(refresh, 3000);
  setInterval(loadToken, 5000);
})();
