(() => {
  const body = document.getElementById("file-body");
  const grid = document.getElementById("grid");
  const table = document.getElementById("file-table");
  const crumbsEl = document.getElementById("crumbs");
  const statusEl = document.getElementById("status");
  const usageEl = document.getElementById("usage");
  const fileInput = document.getElementById("file-input");
  const dropzone = document.getElementById("dropzone");
  const dialog = document.getElementById("prompt-dialog");
  const promptTitle = document.getElementById("prompt-title");
  const promptInput = document.getElementById("prompt-input");
  const promptForm = document.getElementById("prompt-form");
  const promptCancel = document.getElementById("prompt-cancel");
  const searchEl = document.getElementById("search");
  const sortEl = document.getElementById("sort");
  const btnView = document.getElementById("btn-view");
  const btnPaste = document.getElementById("btn-paste");
  const btnZip = document.getElementById("btn-zip");
  const btnBulkDel = document.getElementById("btn-bulk-del");
  const checkAll = document.getElementById("check-all");
  const previewDlg = document.getElementById("preview-dialog");
  const previewName = document.getElementById("preview-name");
  const previewBody = document.getElementById("preview-body");
  const editDlg = document.getElementById("edit-dialog");
  const editForm = document.getElementById("edit-form");
  const editName = document.getElementById("edit-name");
  const editText = document.getElementById("edit-text");
  const shareDlg = document.getElementById("share-dialog");
  const shareUrl = document.getElementById("share-url");
  const shareHint = document.getElementById("share-hint");
  const localDlg = document.getElementById("local-dialog");
  const localHint = document.getElementById("local-hint");
  const localDevices = document.getElementById("local-devices");

  let currentPath = "";
  let mode = "browse"; // browse | search | favs | recent | trash
  let view = localStorage.getItem("cloud-view") || "list";
  let items = [];
  let selected = new Set();
  let clip = null; // {op, paths}
  let editPath = "";
  let favs = new Set();

  function setStatus(msg, isError = false) {
    statusEl.textContent = msg || "";
    statusEl.style.color = isError ? "var(--danger)" : "";
  }

  function fmtSize(n) {
    if (!n) return "—";
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    let v = n;
    while (v >= 1024 && i < u.length - 1) {
      v /= 1024;
      i += 1;
    }
    return `${v < 10 && i > 0 ? v.toFixed(1) : Math.round(v)} ${u[i]}`;
  }

  function esc(s) {
    return String(s)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function fd(obj) {
    const f = new FormData();
    for (const [k, v] of Object.entries(obj)) f.append(k, v);
    return f;
  }

  async function api(url, options) {
    const res = await fetch(url, options);
    if (res.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `请求失败 (${res.status})`);
    return data;
  }

  function askName(title, initial = "") {
    return new Promise((resolve) => {
      promptTitle.textContent = title;
      promptInput.value = initial;
      const onClose = () => {
        dialog.removeEventListener("close", onClose);
        resolve(dialog.returnValue === "ok" ? promptInput.value.trim() : null);
      };
      dialog.addEventListener("close", onClose);
      dialog.showModal();
      promptInput.focus();
      promptInput.select();
    });
  }

  promptInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      if (promptInput.reportValidity()) promptForm.requestSubmit();
    }
  });
  promptCancel.addEventListener("click", () => dialog.close("cancel"));

  function applyView() {
    const gridOn = view === "grid" && mode !== "trash";
    table.hidden = gridOn;
    grid.hidden = !gridOn;
    btnView.textContent = gridOn ? "列表" : "网格";
    localStorage.setItem("cloud-view", view);
  }

  function syncBulk() {
    const n = selected.size;
    btnZip.hidden = n === 0 || mode === "trash";
    btnBulkDel.hidden = n === 0;
    btnPaste.hidden = !clip || mode !== "browse";
    if (checkAll) checkAll.checked = items.length > 0 && items.every((it) => selected.has(it.path || it.trash_id));
  }

  function iconOf(it) {
    if (it.is_dir) return '<svg class="ic"><use href="#i-folder"/></svg>';
    const map = { image: "i-image", video: "i-video", audio: "i-audio", pdf: "i-pdf", text: "i-text", zip: "i-zip" };
    return `<svg class="ic"><use href="#${map[it.kind] || "i-file"}"/></svg>`;
  }

  function keyOf(it) {
    return mode === "trash" ? it.trash_id : it.path;
  }

  function renderCrumbs(crumbs) {
    if (mode !== "browse") {
      const titles = { search: "搜索结果", favs: "收藏", recent: "最近", trash: "回收站" };
      crumbsEl.innerHTML = `<a href="#" data-path="">根目录</a><span class="sep">/</span><span>${titles[mode]}</span>`;
      return;
    }
    crumbsEl.innerHTML = (crumbs || [])
      .map((c, i, arr) => {
        const label = esc(c.name);
        if (i === arr.length - 1) return `<span>${label}</span>`;
        return `<a href="#" data-path="${esc(c.path)}">${label}</a><span class="sep">/</span>`;
      })
      .join("");
  }

  function actions(it) {
    if (mode === "trash") {
      return `<button type="button" class="btn" data-restore="${esc(it.trash_id)}">还原</button>
        <button type="button" class="btn btn-danger" data-purge="${esc(it.trash_id)}" data-name="${esc(it.name)}">彻底删除</button>`;
    }
    const open = it.is_dir
      ? `<button type="button" class="btn" data-open="${esc(it.path)}">打开</button>`
      : `<button type="button" class="btn" data-preview="${esc(it.path)}" data-kind="${esc(it.kind)}" data-name="${esc(it.name)}">预览</button>`;
    const dl = `<a class="btn" href="/api/download?path=${encodeURIComponent(it.path)}">下载</a>`;
    const edit = it.kind === "text"
      ? `<button type="button" class="btn" data-edit="${esc(it.path)}" data-name="${esc(it.name)}">编辑</button>`
      : "";
    const unzip = it.kind === "zip"
      ? `<button type="button" class="btn" data-unzip="${esc(it.path)}">解压</button>`
      : "";
    const send = it.is_dir
      ? ""
      : `<button type="button" class="btn" data-local="${esc(it.path)}" data-name="${esc(it.name)}">发送到设备</button>`;
    const star = favs.has(it.path) ? "取消收藏" : "收藏";
    return `${open}${dl}${edit}${unzip}${send}
      <button type="button" class="btn" data-rename="${esc(it.path)}" data-name="${esc(it.name)}">重命名</button>
      <button type="button" class="btn" data-copy="${esc(it.path)}">复制</button>
      <button type="button" class="btn" data-cut="${esc(it.path)}">剪切</button>
      <button type="button" class="btn" data-share="${esc(it.path)}" data-name="${esc(it.name)}">分享</button>
      <button type="button" class="btn" data-star="${esc(it.path)}">${star}</button>
      <button type="button" class="btn btn-danger" data-del="${esc(it.path)}" data-name="${esc(it.name)}">删除</button>`;
  }

  function render() {
    applyView();
    if (!items.length) {
      body.innerHTML = `<tr><td colspan="5" class="muted">这里是空的</td></tr>`;
      grid.innerHTML = `<p class="muted" style="padding:1rem">这里是空的</p>`;
      syncBulk();
      return;
    }
    body.innerHTML = items
      .map((it) => {
        const k = keyOf(it);
        const nameHtml = it.is_dir && mode !== "trash"
          ? `<a class="name-link" href="#" data-open="${esc(it.path)}">${esc(it.name)}</a>`
          : mode === "trash"
            ? `<span>${esc(it.name)}</span>`
            : `<a class="name-link" href="#" data-preview="${esc(it.path)}" data-kind="${esc(it.kind)}" data-name="${esc(it.name)}">${esc(it.name)}</a>`;
        const extra = mode === "trash" && it.orig ? `<div class="muted">${esc(it.orig)}</div>` : "";
        return `<tr>
          <td class="col-check"><input type="checkbox" data-check="${esc(k)}" ${selected.has(k) ? "checked" : ""} /></td>
          <td><div class="name-cell"><span class="icon t-${it.kind}">${iconOf(it)}</span><div>${nameHtml}${extra}</div></div></td>
          <td class="col-size">${it.is_dir ? "—" : fmtSize(it.size)}</td>
          <td class="col-time">${esc(it.mtime)}</td>
          <td class="col-actions"><div class="row-actions">${actions(it)}</div></td>
        </tr>`;
      })
      .join("");
    grid.innerHTML = items
      .map((it) => {
        const k = keyOf(it);
        return `<div class="card" data-card="${esc(it.path || "")}" data-kind="${esc(it.kind || "")}" data-dir="${it.is_dir ? "1" : "0"}">
          <input type="checkbox" data-check="${esc(k)}" ${selected.has(k) ? "checked" : ""} />
          <span class="icon t-${it.kind}">${iconOf(it)}</span>
          <div class="nm">${esc(it.name)}</div>
        </div>`;
      })
      .join("");
    syncBulk();
  }

  async function refreshStats() {
    try {
      const s = await api("/api/stats");
      usageEl.textContent = `${fmtSize(s.used)} · ${s.files} 个文件 · 回收站 ${s.trash}`;
      usageEl.title = `磁盘剩余 ${fmtSize(s.disk_free)} / ${fmtSize(s.disk_total)}`;
    } catch {
      /* ignore */
    }
  }

  async function loadFavs() {
    try {
      const data = await api("/api/favorites");
      favs = new Set((data.items || []).map((x) => x.path));
    } catch {
      favs = new Set();
    }
  }

  async function load(path = currentPath) {
    mode = "browse";
    currentPath = path || "";
    selected = new Set();
    setStatus("加载中…");
    try {
      await loadFavs();
      const sort = sortEl.value || "name";
      const data = await api(`/api/list?path=${encodeURIComponent(currentPath)}&sort=${encodeURIComponent(sort)}`);
      currentPath = data.path || "";
      items = data.items || [];
      renderCrumbs(data.crumbs);
      render();
      setStatus(`${items.length} 项`);
      history.replaceState(null, "", `#${encodeURIComponent(currentPath)}`);
      refreshStats();
    } catch (err) {
      setStatus(err.message || String(err), true);
    }
  }

  async function loadSpecial(which, url, label) {
    mode = which;
    selected = new Set();
    setStatus("加载中…");
    try {
      await loadFavs();
      const data = await api(url);
      items = data.items || [];
      renderCrumbs([]);
      render();
      setStatus(`${label} · ${items.length} 项`);
    } catch (err) {
      setStatus(err.message || String(err), true);
    }
  }

  async function doSearch() {
    const q = searchEl.value.trim();
    if (!q) {
      await load(currentPath);
      return;
    }
    mode = "search";
    selected = new Set();
    setStatus("搜索中…");
    try {
      await loadFavs();
      const data = await api(`/api/search?q=${encodeURIComponent(q)}`);
      items = data.items || [];
      renderCrumbs([]);
      render();
      setStatus(`搜索「${q}」· ${items.length} 项${data.capped ? "（已截断）" : ""}`);
    } catch (err) {
      setStatus(err.message || String(err), true);
    }
  }

  async function uploadFiles(fileList) {
    const files = [...fileList];
    if (!files.length || mode !== "browse") return;
    const form = fd({ path: currentPath });
    const rels = [];
    for (const f of files) {
      form.append("files", f);
      rels.push(f.webkitRelativePath || "");
    }
    if (rels.some(Boolean)) {
      for (const r of rels) form.append("rel_paths", r);
    }
    setStatus(`正在上传 ${files.length} 个文件…`);
    try {
      const data = await api("/api/upload", { method: "POST", body: form });
      setStatus(`已上传 ${data.items.length} 个文件`);
      await load(currentPath);
    } catch (err) {
      setStatus(err.message || String(err), true);
    }
  }

  function setClip(op, paths) {
    clip = { op, paths };
    setStatus(op === "cut" ? `已剪切 ${paths.length} 项，到目标目录点粘贴` : `已复制 ${paths.length} 项，到目标目录点粘贴`);
    syncBulk();
  }

  async function pasteHere() {
    if (!clip || mode !== "browse") return;
    const ep = clip.op === "cut" ? "/api/move" : "/api/copy";
    try {
      for (const src of clip.paths) {
        await api(ep, { method: "POST", body: fd({ src, dest: currentPath }) });
      }
      setStatus("已粘贴");
      clip = clip.op === "cut" ? null : clip;
      await load(currentPath);
    } catch (err) {
      setStatus(err.message || String(err), true);
    }
  }

  async function preview(path, kind, name) {
    previewName.textContent = name || path;
    previewBody.innerHTML = "";
    const src = `/api/raw?path=${encodeURIComponent(path)}`;
    if (kind === "image") previewBody.innerHTML = `<img src="${src}" alt="" />`;
    else if (kind === "video") previewBody.innerHTML = `<video src="${src}" controls autoplay></video>`;
    else if (kind === "audio") previewBody.innerHTML = `<audio src="${src}" controls autoplay></audio>`;
    else if (kind === "pdf") previewBody.innerHTML = `<iframe src="${src}" style="width:100%;height:70vh;border:0"></iframe>`;
    else if (kind === "text") {
      const data = await api(`/api/text?path=${encodeURIComponent(path)}`);
      previewBody.innerHTML = `<pre>${esc(data.text)}</pre>`;
    } else {
      previewBody.innerHTML = `<p class="muted">无法预览，请下载。</p><p><a class="btn btn-primary" href="/api/download?path=${encodeURIComponent(path)}">下载</a></p>`;
    }
    previewDlg.showModal();
  }

  async function openEdit(path, name) {
    const data = await api(`/api/text?path=${encodeURIComponent(path)}`);
    editPath = path;
    editName.textContent = name || path;
    editText.value = data.text;
    editDlg.showModal();
    editText.focus();
  }

  function deviceMeta(d) {
    return [d.model, d.type, d.ip].filter(Boolean).join(" · ");
  }

  async function openLocalSend(path, name) {
    localHint.textContent = `发送「${name}」到附近设备（需对方打开 LocalSend）`;
    localDevices.innerHTML = `<p class="muted">正在搜索局域网设备…</p>`;
    localDlg.showModal();
    try {
      const data = await api("/api/local/discover");
      const devs = data.devices || [];
      if (!devs.length) {
        localDevices.innerHTML = `<p class="muted">没有发现附近的 LocalSend 设备。<br />请确认接收设备已打开 LocalSend，并与本机连接同一 WiFi/路由器。</p>`;
        return;
      }
      localDevices.innerHTML = devs
        .map(
          (d) => `<button type="button" class="device-item" data-send-host="${esc(d.ip)}" data-port="${esc(d.port || 53317)}" data-https="${d.protocol === 'https' ? '1' : ''}" data-alias="${esc(d.alias)}">
            <svg class="ic" aria-hidden="true"><use href="#i-send"/></svg>
            <span><span class="dv-name">${esc(d.alias)}</span><br /><span class="dv-meta">${esc(deviceMeta(d))}</span></span>
          </button>`
        )
        .join("");
      localDevices.querySelectorAll("[data-send-host]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const alias = btn.getAttribute("data-alias") || btn.getAttribute("data-send-host");
          localDevices.innerHTML = `<p class="muted">等待「${esc(alias)}」在 LocalSend 中确认…</p>`;
          localHint.textContent = `正在发送「${name}」到 ${alias}…`;
          try {
            await api("/api/local/send", { method: "POST", body: fd({ path, host: btn.getAttribute("data-send-host"), port: btn.getAttribute("data-port"), use_https: btn.getAttribute("data-https") }) });
            localDlg.close();
            setStatus(`已发送「${name}」到 ${alias}`);
          } catch (err) {
            localDevices.innerHTML = `<p class="error">${esc(err.message)}</p><p class="muted">对方需在 LocalSend 中点击接收，并保持与本机同一网络。</p>`;
            localHint.textContent = `发送「${name}」到 ${alias} 失败`;
          }
        });
      });
    } catch (err) {
      localDevices.innerHTML = `<p class="error">${esc(err.message)}</p>`;
    }
  }

  async function handle(el) {
    if (el.dataset.open != null) {
      await load(el.dataset.open);
      return;
    }
    if (el.dataset.preview != null) {
      await preview(el.dataset.preview, el.dataset.kind, el.dataset.name);
      return;
    }
    if (el.dataset.edit != null) {
      await openEdit(el.dataset.edit, el.dataset.name);
      return;
    }
    if (el.dataset.rename != null) {
      const name = await askName("重命名", el.dataset.name || "");
      if (!name || name === el.dataset.name) return;
      await api("/api/rename", { method: "POST", body: fd({ path: el.dataset.rename, name }) });
      await load(currentPath);
      return;
    }
    if (el.dataset.copy != null) {
      setClip("copy", [el.dataset.copy]);
      return;
    }
    if (el.dataset.cut != null) {
      setClip("cut", [el.dataset.cut]);
      return;
    }
    if (el.dataset.share != null) {
      const data = await api("/api/share", { method: "POST", body: fd({ path: el.dataset.share, days: "7" }) });
      shareHint.textContent = `「${el.dataset.name}」7 天内有效`;
      shareUrl.value = location.origin + data.url;
      shareDlg.showModal();
      shareUrl.focus();
      shareUrl.select();
      return;
    }
    if (el.dataset.local != null) {
      await openLocalSend(el.dataset.local, el.dataset.name);
      return;
    }
    if (el.dataset.star != null) {
      await api("/api/favorites", { method: "POST", body: fd({ path: el.dataset.star }) });
      if (mode === "favs") await loadSpecial("favs", "/api/favorites", "收藏");
      else await load(currentPath);
      return;
    }
    if (el.dataset.unzip != null) {
      await api("/api/unzip", { method: "POST", body: fd({ path: el.dataset.unzip }) });
      await load(currentPath);
      return;
    }
    if (el.dataset.del != null) {
      if (!confirm(`删除「${el.dataset.name}」到回收站？`)) return;
      await api("/api/delete", { method: "POST", body: fd({ path: el.dataset.del }) });
      if (mode === "search") await doSearch();
      else if (mode === "favs") await loadSpecial("favs", "/api/favorites", "收藏");
      else if (mode === "recent") await loadSpecial("recent", "/api/recent", "最近");
      else await load(currentPath);
      return;
    }
    if (el.dataset.restore != null) {
      await api("/api/trash/restore", { method: "POST", body: fd({ trash_id: el.dataset.restore }) });
      await loadSpecial("trash", "/api/trash", "回收站");
      return;
    }
    if (el.dataset.purge != null) {
      if (!confirm(`彻底删除「${el.dataset.name}」？不可恢复。`)) return;
      await api("/api/trash/purge", { method: "POST", body: fd({ trash_id: el.dataset.purge }) });
      await loadSpecial("trash", "/api/trash", "回收站");
    }
  }

  document.getElementById("btn-mkdir").addEventListener("click", async () => {
    const name = await askName("新建文件夹");
    if (!name) return;
    try {
      await api("/api/mkdir", { method: "POST", body: fd({ path: currentPath, name }) });
      await load(currentPath);
    } catch (err) {
      setStatus(err.message, true);
    }
  });

  document.getElementById("btn-touch").addEventListener("click", async () => {
    const name = await askName("新建文本文件", "未命名.txt");
    if (!name) return;
    try {
      await api("/api/touch", { method: "POST", body: fd({ path: currentPath, name }) });
      await load(currentPath);
    } catch (err) {
      setStatus(err.message, true);
    }
  });

  const themeBtn = document.getElementById("btn-theme");
  const themeLink = document.getElementById("theme-css");
  function applyTheme(t) {
    themeLink.href = t === "v1" ? "/static/app.css" : "/static/app-v2.css";
    try { localStorage.setItem("theme", t); } catch (e) {}
  }
  if (themeBtn) {
    themeBtn.addEventListener("click", () => {
      let cur = "v2";
      try { cur = localStorage.getItem("theme") || "v2"; } catch (e) {}
      applyTheme(cur === "v1" ? "v2" : "v1");
    });
  }

  const darkBtn = document.getElementById("btn-dark");
  function currentDark() {
    try { return localStorage.getItem("dark") === "1"; } catch (e) { return false; }
  }
  function applyDark(d) {
    document.documentElement.classList.toggle("dark", d);
    try { localStorage.setItem("dark", d ? "1" : "0"); } catch (e) {}
    if (darkBtn) {
      const use = darkBtn.querySelector("use");
      const span = darkBtn.querySelector("span");
      if (use) use.setAttribute("href", d ? "#i-sun" : "#i-moon");
      if (span) span.textContent = d ? "浅色" : "深色";
    }
  }
  if (darkBtn) {
    darkBtn.addEventListener("click", () => applyDark(!currentDark()));
    applyDark(currentDark());
  }

  fileInput.addEventListener("change", () => {
    uploadFiles(fileInput.files);
    fileInput.value = "";
  });

  const folderInput = document.getElementById("folder-input");
  folderInput.addEventListener("change", () => {
    uploadFiles(folderInput.files);
    folderInput.value = "";
  });

  searchEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      doSearch();
    }
  });

  sortEl.addEventListener("change", () => {
    if (mode === "browse") load(currentPath);
  });
  btnView.addEventListener("click", () => {
    view = view === "grid" ? "list" : "grid";
    render();
  });
  document.getElementById("btn-favs").addEventListener("click", () => loadSpecial("favs", "/api/favorites", "收藏"));
  document.getElementById("btn-recent").addEventListener("click", () => loadSpecial("recent", "/api/recent", "最近"));
  document.getElementById("btn-trash").addEventListener("click", () => loadSpecial("trash", "/api/trash", "回收站"));
  btnPaste.addEventListener("click", pasteHere);

  btnZip.addEventListener("click", async () => {
    if (!selected.size) return;
    const form = fd({ paths: [...selected].join("\n") });
    const res = await fetch("/api/zip", { method: "POST", body: form });
    if (res.status === 401) location.href = "/login";
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      setStatus(data.detail || "打包失败", true);
      return;
    }
    const blob = await res.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "download.zip";
    a.click();
  });

  btnBulkDel.addEventListener("click", async () => {
    if (!selected.size) return;
    if (!confirm(`删除所选 ${selected.size} 项？`)) return;
    try {
      if (mode === "trash") {
        for (const id of selected) await api("/api/trash/purge", { method: "POST", body: fd({ trash_id: id }) });
        await loadSpecial("trash", "/api/trash", "回收站");
      } else {
        for (const p of selected) await api("/api/delete", { method: "POST", body: fd({ path: p }) });
        if (mode === "browse") await load(currentPath);
        else if (mode === "search") await doSearch();
        else if (mode === "favs") await loadSpecial("favs", "/api/favorites", "收藏");
        else await loadSpecial("recent", "/api/recent", "最近");
      }
    } catch (err) {
      setStatus(err.message, true);
    }
  });

  checkAll.addEventListener("change", () => {
    if (checkAll.checked) items.forEach((it) => selected.add(keyOf(it)));
    else selected.clear();
    render();
  });

  function onPanelClick(e) {
    const box = e.target.closest("[data-check]");
    if (box) {
      const k = box.getAttribute("data-check");
      if (box.checked) selected.add(k);
      else selected.delete(k);
      syncBulk();
      return;
    }
    const card = e.target.closest("[data-card]");
    if (card && !e.target.closest("input")) {
      e.preventDefault();
      if (mode === "trash") return;
      if (card.dataset.dir === "1") load(card.dataset.card);
      else preview(card.dataset.card, card.dataset.kind, card.querySelector(".nm")?.textContent);
      return;
    }
    const act = e.target.closest("[data-open],[data-preview],[data-edit],[data-rename],[data-copy],[data-cut],[data-share],[data-local],[data-star],[data-unzip],[data-del],[data-restore],[data-purge]");
    if (!act) return;
    e.preventDefault();
    handle(act).catch((err) => setStatus(err.message || String(err), true));
  }

  body.addEventListener("click", onPanelClick);
  grid.addEventListener("click", onPanelClick);

  crumbsEl.addEventListener("click", (e) => {
    const a = e.target.closest("a[data-path]");
    if (!a) return;
    e.preventDefault();
    load(a.getAttribute("data-path") || "");
  });

  document.getElementById("preview-close").addEventListener("click", () => previewDlg.close());
  previewDlg.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      previewDlg.close();
    }
  });

  editForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/text", { method: "POST", body: fd({ path: editPath, text: editText.value }) });
      editDlg.close();
      setStatus("已保存");
    } catch (err) {
      setStatus(err.message, true);
    }
  });
  document.getElementById("edit-cancel").addEventListener("click", () => editDlg.close());
  editText.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      editForm.requestSubmit();
    }
  });

  async function copyShare() {
    try {
      await navigator.clipboard.writeText(shareUrl.value);
      setStatus("链接已复制");
    } catch {
      shareUrl.select();
      document.execCommand("copy");
    }
    shareDlg.close();
  }
  document.getElementById("share-form").addEventListener("submit", (e) => {
    e.preventDefault();
    copyShare();
  });
  document.getElementById("share-copy").addEventListener("click", (e) => {
    e.preventDefault();
    copyShare();
  });
  document.getElementById("share-close").addEventListener("click", () => shareDlg.close());
  document.getElementById("local-close").addEventListener("click", () => localDlg.close());

  function collectDroppedFiles(dt) {
    const files = [...(dt.files || [])];
    if (files.length) return Promise.resolve(files);
    const entries = [...(dt.items || [])]
      .map((it) => it.webkitGetAsEntry && it.webkitGetAsEntry())
      .filter(Boolean);
    if (!entries.length) return Promise.resolve(files);
    const out = [];
    const walk = (entry, base) => new Promise((resolve) => {
      if (entry.isFile) {
        entry.file((file) => {
          Object.defineProperty(file, "webkitRelativePath", {
            value: base ? `${base}/${file.name}` : file.name,
          });
          out.push(file);
          resolve();
        }, resolve);
      } else if (entry.isDirectory) {
        const reader = entry.createReader();
        const base2 = base ? `${base}/${entry.name}` : entry.name;
        const readBatch = () =>
          reader.readEntries((es) => {
            if (!es.length) return resolve();
            Promise.all(es.map((e) => walk(e, base2))).then(readBatch);
          }, resolve);
        readBatch();
      } else {
        resolve();
      }
    });
    return Promise.all(entries.map((e) => walk(e, ""))).then(() => out);
  }

  let dragDepth = 0;
  window.addEventListener("dragenter", (e) => {
    e.preventDefault();
    dragDepth += 1;
    dropzone.hidden = false;
  });
  window.addEventListener("dragleave", (e) => {
    e.preventDefault();
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) dropzone.hidden = true;
  });
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", async (e) => {
    e.preventDefault();
    dragDepth = 0;
    dropzone.hidden = true;
    if (!e.dataTransfer) return;
    const files = await collectDroppedFiles(e.dataTransfer);
    if (files.length) uploadFiles(files);
  });

  const hashPath = decodeURIComponent((location.hash || "#").slice(1));
  load(hashPath);
})();
