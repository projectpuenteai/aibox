const APP_API = "/ai/api/v1/app";
const STORAGE_KEYS = {
  language: "puente-language",
  theme: "puente-theme"
};
const md = typeof window.markdownit === "function" ? window.markdownit({ html: true, linkify: true, breaks: true }) : null;

const copy = {
  en: {
    appTitle: "Project Puente Docs",
    workspace: "Workspace",
    docs: "Docs",
    loadingWorkspace: "Loading workspace...",
    home: "Home",
    searchDocuments: "Search documents",
    newDocument: "+ New document",
    starred: "Starred",
    starredDescription: "Protected documents stay here for quick access.",
    allDocuments: "All documents",
    mostRecentFirst: "Most recently edited first.",
    trash: "Trash",
    trashDescription: "Deleted documents can be restored from here.",
    untitledDocument: "Untitled document",
    allChangesSaved: "All changes saved",
    loadingDocument: "Loading document...",
    star: "\u2606 Star",
    starredButton: "\u2605 Starred",
    moveToTrash: "Move to trash",
    restore: "Restore",
    trashView: "Trash view",
    trashReadOnly: "This document is read only until you restore it.",
    font: "Font",
    size: "Size",
    text: "Text",
    highlight: "Highlight",
    clear: "Clear",
    editorPlaceholder: "You can write here.",
    loadingDocs: "Loading docs...",
    signedInAs: "Signed in as {username}{role}",
    roleSuffix: " ({role})",
    activeTab: "Active ({count})",
    trashTab: "Trash ({count})",
    documentsInTrash: "{count} document{suffix} in trash",
    activeDocuments: "{count} active document{suffix}",
    suffixPlural: "s",
    storedInTrash: "Stored in trash until restored.",
    openToStartWriting: "Open this document to start writing.",
    deletedAt: "Deleted {date}",
    editedAt: "Edited {date}",
    protected: "Protected",
    open: "Open",
    delete: "Delete",
    noStarredDocs: "Star documents to keep them protected and easy to find.",
    trashEmpty: "Trash is empty.",
    noDocumentsFound: "No documents found.",
    readOnly: "Read only",
    saving: "Saving...",
    savingChanges: "Saving changes...",
    createdAt: "Created {date}",
    unstarDocument: "Unstar document",
    starDocument: "Star document",
    starredMustUnstar: "Starred documents must be unstarred before deletion.",
    continuingSave: "Continuing to save changes...",
    saved: "Saved.",
    failedSave: "Failed to save document",
    documentReady: "Document ready.",
    openedTrashDocument: "Opened trash document.",
    failedLoadDocument: "Failed to load document",
    backToDashboard: "Back to dashboard.",
    creatingDocument: "Creating document...",
    documentCreationMissingId: "Document creation did not return an id.",
    failedCreateDocument: "Failed to create document",
    titleUpdated: "Title updated.",
    failedRenameDocument: "Failed to rename document",
    documentStarredProtected: "Document starred and protected.",
    documentUnstarred: "Document unstarred.",
    failedUpdateStar: "Failed to update star",
    documentMovedToTrash: "Document moved to trash.",
    failedDeleteDocument: "Failed to delete document",
    documentRestored: "Document restored.",
    documentRestoredActive: "Document restored to active docs.",
    failedRestoreDocument: "Failed to restore document",
    clearTrash: "Clear trash",
    clearTrashConfirm: "Delete every document in trash permanently?",
    trashCleared: "Trash cleared.",
    trashAlreadyEmpty: "Trash is already empty.",
    failedClearTrash: "Failed to clear trash",
    checklistItem: "List item",
    ready: "Ready.",
    failedLoadDocs: "Failed to load docs",
    failedInitDocs: "Failed to initialize docs",
    pasteCooldown: "Please wait 5 seconds between pastes.",
    pasteDuplicate: "You've pasted this content too many times.",
    pasteTooLong: "{count} / 7500 characters — paste is too long."
  },
  es: {
    appTitle: "Project Puente Docs",
    workspace: "Espacio de Trabajo",
    docs: "Docs",
    loadingWorkspace: "Cargando espacio de trabajo...",
    home: "Inicio",
    searchDocuments: "Buscar documentos",
    newDocument: "+ Nuevo documento",
    starred: "Destacados",
    starredDescription: "Los documentos protegidos se quedan aqui para acceso rapido.",
    allDocuments: "Todos los documentos",
    mostRecentFirst: "Los editados mas recientemente primero.",
    trash: "Papelera",
    trashDescription: "Los documentos eliminados se pueden restaurar desde aqui.",
    untitledDocument: "Documento sin titulo",
    allChangesSaved: "Todos los cambios guardados",
    loadingDocument: "Cargando documento...",
    star: "\u2606 Destacar",
    starredButton: "\u2605 Destacado",
    moveToTrash: "Mover a la papelera",
    restore: "Restaurar",
    trashView: "Vista de papelera",
    trashReadOnly: "Este documento es de solo lectura hasta que lo restaures.",
    font: "Fuente",
    size: "Tamano",
    text: "Texto",
    highlight: "Resaltado",
    clear: "Limpiar",
    editorPlaceholder: "Puedes escribir aqui.",
    loadingDocs: "Cargando docs...",
    signedInAs: "Sesion iniciada como {username}{role}",
    roleSuffix: " ({role})",
    activeTab: "Activos ({count})",
    trashTab: "Papelera ({count})",
    documentsInTrash: "{count} documento{suffix} en la papelera",
    activeDocuments: "{count} documento{suffix} activo{suffix}",
    suffixPlural: "s",
    storedInTrash: "Guardado en la papelera hasta que se restaure.",
    openToStartWriting: "Abre este documento para empezar a escribir.",
    deletedAt: "Eliminado {date}",
    editedAt: "Editado {date}",
    protected: "Protegido",
    open: "Abrir",
    delete: "Eliminar",
    noStarredDocs: "Destaca documentos para mantenerlos protegidos y faciles de encontrar.",
    trashEmpty: "La papelera esta vacia.",
    noDocumentsFound: "No se encontraron documentos.",
    readOnly: "Solo lectura",
    saving: "Guardando...",
    savingChanges: "Guardando cambios...",
    createdAt: "Creado {date}",
    unstarDocument: "Quitar destacado",
    starDocument: "Destacar documento",
    starredMustUnstar: "Los documentos destacados deben quitarse de destacados antes de eliminarlos.",
    continuingSave: "Continuando con el guardado de cambios...",
    saved: "Guardado.",
    failedSave: "No se pudo guardar el documento",
    documentReady: "Documento listo.",
    openedTrashDocument: "Documento de papelera abierto.",
    failedLoadDocument: "No se pudo cargar el documento",
    backToDashboard: "Volver al panel.",
    creatingDocument: "Creando documento...",
    documentCreationMissingId: "La creacion del documento no devolvio un id.",
    failedCreateDocument: "No se pudo crear el documento",
    titleUpdated: "Titulo actualizado.",
    failedRenameDocument: "No se pudo cambiar el titulo",
    documentStarredProtected: "Documento destacado y protegido.",
    documentUnstarred: "Documento sin destacar.",
    failedUpdateStar: "No se pudo actualizar el destacado",
    documentMovedToTrash: "Documento movido a la papelera.",
    failedDeleteDocument: "No se pudo eliminar el documento",
    documentRestored: "Documento restaurado.",
    documentRestoredActive: "Documento restaurado a los documentos activos.",
    failedRestoreDocument: "No se pudo restaurar el documento",
    clearTrash: "Vaciar papelera",
    clearTrashConfirm: "¿Eliminar permanentemente todos los documentos de la papelera?",
    trashCleared: "Papelera vaciada.",
    trashAlreadyEmpty: "La papelera ya esta vacia.",
    failedClearTrash: "No se pudo vaciar la papelera",
    checklistItem: "Elemento de lista",
    ready: "Listo.",
    failedLoadDocs: "No se pudieron cargar los docs",
    failedInitDocs: "No se pudo iniciar Docs",
    pasteCooldown: "Por favor espera 5 segundos entre pegadas.",
    pasteDuplicate: "Has pegado este contenido demasiadas veces.",
    pasteTooLong: "{count} / 7500 caracteres — el texto pegado es demasiado largo."
  },
};

const ui = {
  dashboardView: document.getElementById("dashboardView"),
  editorView: document.getElementById("editorView"),
  workspaceEyebrow: document.getElementById("workspaceEyebrow"),
  docsHeroTitle: document.getElementById("docsHeroTitle"),
  homeLink: document.getElementById("homeLink"),
  userMeta: document.getElementById("userMeta"),
  searchInput: document.getElementById("searchInput"),
  newDocBtn: document.getElementById("newDocBtn"),
  allDocsNewBtn: document.getElementById("allDocsNewBtn"),
  scopeTabs: document.getElementById("scopeTabs"),
  docSummary: document.getElementById("docSummary"),
  starredSection: document.getElementById("starredSection"),
  starredHeading: document.getElementById("starredHeading"),
  starredDescription: document.getElementById("starredDescription"),
  starredDocs: document.getElementById("starredDocs"),
  allDocsHeading: document.getElementById("allDocsHeading"),
  allDocsMeta: document.getElementById("allDocsMeta"),
  allDocsGrid: document.getElementById("allDocsGrid"),
  clearTrashBtn: document.getElementById("clearTrashBtn"),
  backBtn: document.getElementById("backBtn"),
  editorTitleInput: document.getElementById("editorTitleInput"),
  editorSaveState: document.getElementById("editorSaveState"),
  editorMeta: document.getElementById("editorMeta"),
  editorStarBtn: document.getElementById("editorStarBtn"),
  editorDeleteBtn: document.getElementById("editorDeleteBtn"),
  editorRestoreBtn: document.getElementById("editorRestoreBtn"),
  trashBanner: document.getElementById("trashBanner"),
  trashBannerTitle: document.getElementById("trashBannerTitle"),
  trashBannerText: document.getElementById("trashBannerText"),
  fontLabel: document.getElementById("fontLabel"),
  fontSelect: document.getElementById("fontSelect"),
  sizeLabel: document.getElementById("sizeLabel"),
  fontSizeSelect: document.getElementById("fontSizeSelect"),
  textColorLabel: document.getElementById("textColorLabel"),
  textColorInput: document.getElementById("textColorInput"),
  highlightLabel: document.getElementById("highlightLabel"),
  highlightColorInput: document.getElementById("highlightColorInput"),
  clearHighlightBtn: document.getElementById("clearHighlightBtn"),
  checklistBtn: document.getElementById("checklistBtn"),
  editorSurface: document.getElementById("editorSurface"),
  statusText: document.getElementById("statusText"),
  warningText: document.getElementById("warningText")
};

const state = {
  language: "en",
  theme: "light",
  user: null,
  docs: [],
  currentDocId: null,
  currentScope: "active",
  query: "",
  dirty: false,
  saving: false,
  saveTimer: null,
  lastLoadedDocId: null,
  lastLoadedHtml: "",
  loadingDoc: false,
  lastPasteTime: 0,
  pasteContentCounts: {}
};

function getCopy() {
  return copy[state.language] || copy.en;
}

function formatText(text, vars = {}) {
  return String(text || "").replace(/\{(\w+)\}/g, (_, key) => String(vars[key] ?? ""));
}

function text(key, vars = {}) {
  return formatText((getCopy()[key] ?? copy.en[key] ?? ""), vars);
}

function applyTheme(theme) {
  state.theme = theme === "dark" ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", state.theme);
}

function syncPreferencesFromUser(user) {
  if (!user) return;
  state.language = user.preferred_language === "es" ? "es" : "en";
  state.theme = user.preferred_theme === "dark" ? "dark" : "light";
  applyTheme(state.theme);
  try {
    localStorage.setItem(STORAGE_KEYS.language, state.language);
    localStorage.setItem(STORAGE_KEYS.theme, state.theme);
  } catch {}
  renderStaticText();
  renderAll();
}

function loadLocalPreferences() {
  try {
    const language = localStorage.getItem(STORAGE_KEYS.language);
    const theme = localStorage.getItem(STORAGE_KEYS.theme);
    if (language === "en" || language === "es") {
      state.language = language;
    }
    if (theme === "light" || theme === "dark") {
      state.theme = theme;
    }
  } catch {}
  applyTheme(state.theme);
  renderStaticText();
}

function renderStaticText() {
  const t = getCopy();
  document.title = t.appTitle;
  document.documentElement.lang = state.language;
  ui.workspaceEyebrow.textContent = t.workspace;
  ui.docsHeroTitle.textContent = t.docs;
  ui.homeLink.textContent = t.home;
  ui.homeLink.setAttribute("aria-label", t.home);
  ui.searchInput.placeholder = t.searchDocuments;
  ui.newDocBtn.textContent = t.newDocument;
  ui.allDocsNewBtn.setAttribute("aria-label", t.newDocument);
  ui.starredHeading.textContent = t.starred;
  ui.starredDescription.textContent = t.starredDescription;
  ui.allDocsHeading.textContent = t.allDocuments;
  ui.allDocsMeta.textContent = t.mostRecentFirst;
  ui.clearTrashBtn.textContent = t.clearTrash;
  ui.docSummary.textContent = t.loadingDocs;
  ui.editorTitleInput.placeholder = t.untitledDocument;
  ui.trashBannerTitle.textContent = t.trashView;
  ui.trashBannerText.textContent = t.trashReadOnly;
  ui.fontLabel.textContent = t.font;
  ui.sizeLabel.textContent = t.size;
  ui.textColorLabel.textContent = t.text;
  ui.highlightLabel.textContent = t.highlight;
  ui.clearHighlightBtn.textContent = t.clear;
  ui.editorSurface.dataset.placeholder = t.editorPlaceholder;
  ui.editorSaveState.textContent = t.allChangesSaved;
  ui.editorMeta.textContent = t.loadingDocument;
  ui.statusText.textContent = t.loadingDocs;
  ui.warningText.textContent = "";
  ui.userMeta.textContent = state.user
    ? text("signedInAs", { username: state.user.username, role: state.user.role ? text("roleSuffix", { role: state.user.role }) : "" })
    : t.loadingWorkspace;
}

function setStatus(message, tone = "") {
  ui.statusText.className = `status-text ${tone}`.trim();
  ui.statusText.textContent = message;
}

function setWarning(message = "") {
  ui.warningText.textContent = message;
}

async function api(path, method = "GET", body = undefined) {
  const config = { method, credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    config.headers["Content-Type"] = "application/json";
    config.body = JSON.stringify(body);
  }
  const response = await fetch(APP_API + path, config);
  const textValue = await response.text();
  let payload = {};
  try {
    payload = textValue ? JSON.parse(textValue) : {};
  } catch {
    payload = {};
  }
  if (!response.ok) {
    const error = new Error(payload.detail || payload.message || textValue || `Request failed (${response.status})`);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/`/g, "&#96;");
}

function formatDate(value) {
  const date = new Date(value || Date.now());
  if (Number.isNaN(date.getTime())) {
    return "recently";
  }
  return date.toLocaleString(state.language === "es" ? "es-ES" : "en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit"
  });
}

function formatStorageWarning(warning) {
  if (!warning || typeof warning !== "object") {
    return "";
  }
  const level = warning.level ? String(warning.level).toUpperCase() : "STORAGE";
  const used = warning.used_percent !== undefined ? `Used ${warning.used_percent}%` : "";
  return [level, used].filter(Boolean).join(" - ");
}

function isProbablyHtml(content) {
  return /<\/?[a-z][\s\S]*>/i.test(String(content || ""));
}

function sanitizeHtml(html) {
  if (window.DOMPurify && typeof window.DOMPurify.sanitize === "function") {
    return window.DOMPurify.sanitize(html, {
      ALLOWED_TAGS: ["a", "b", "blockquote", "br", "div", "em", "font", "hr", "i", "input", "label", "li", "ol", "p", "span", "strike", "strong", "table", "tbody", "td", "th", "thead", "tr", "u", "ul"],
      ALLOWED_ATTR: ["checked", "class", "data-type", "face", "href", "rel", "size", "style", "target", "type"]
    });
  }
  return html;
}

function storedContentToHtml(content) {
  const raw = String(content || "").trim();
  if (!raw) {
    return "<div><br></div>";
  }
  if (isProbablyHtml(raw)) {
    return sanitizeHtml(raw);
  }
  if (md) {
    return sanitizeHtml(md.render(raw));
  }
  return `<p>${escapeHtml(raw).replace(/\n/g, "<br>")}</p>`;
}

function normalizeEditorHtml(html) {
  const wrapper = document.createElement("div");
  wrapper.innerHTML = sanitizeHtml(String(html || ""));
  wrapper.querySelectorAll("script,style").forEach(node => node.remove());
  wrapper.querySelectorAll("a").forEach(link => {
    link.setAttribute("target", "_blank");
    link.setAttribute("rel", "noopener noreferrer");
  });
  return wrapper.innerHTML.trim() || "<div><br></div>";
}

function normalizeEditorLists() {
  const editor = ui.editorSurface;
  if (!editor) {
    return;
  }
  editor.querySelectorAll("ul:not([data-type='checklist'])").forEach(list => {
    list.classList.add("editor-ul");
  });
  editor.querySelectorAll("ol").forEach(list => {
    list.classList.add("editor-ol");
  });
  editor.querySelectorAll("ul[data-type='checklist']").forEach(list => {
    list.classList.remove("editor-ul");
  });
}

function editorHtmlToStoredContent() {
  normalizeEditorLists();
  return normalizeEditorHtml(ui.editorSurface.innerHTML);
}

function extractPreviewText(doc) {
  const t = getCopy();
  const source = String(doc.content_html || doc.content_markdown || "");
  if (!source) {
    return doc.is_deleted ? t.storedInTrash : t.openToStartWriting;
  }
  const textContent = source.replace(/<[^>]+>/g, " ").replace(/&nbsp;/g, " ").replace(/\s+/g, " ").trim();
  if (!textContent) {
    return doc.is_deleted ? t.storedInTrash : t.openToStartWriting;
  }
  return textContent.slice(0, 88) + (textContent.length > 88 ? "..." : "");
}

function mergeDoc(docId, updates) {
  state.docs = state.docs.map(doc => String(doc.id) === String(docId) ? { ...doc, ...updates } : doc);
}

function sortDocs(docs) {
  return [...docs].sort((left, right) => {
    const leftTs = Date.parse(left.updated_at || left.created_at || 0) || 0;
    const rightTs = Date.parse(right.updated_at || right.created_at || 0) || 0;
    return rightTs - leftTs;
  });
}

function getDoc(docId) {
  return state.docs.find(doc => String(doc.id) === String(docId)) || null;
}

function getCurrentDoc() {
  return getDoc(state.currentDocId);
}

function getVisibleDocs() {
  const docs = state.docs.filter(doc => state.currentScope === "trash" ? !!doc.is_deleted : !doc.is_deleted);
  const query = state.query.trim().toLowerCase();
  const filtered = query ? docs.filter(doc => String(doc.title || "").toLowerCase().includes(query)) : docs;
  return sortDocs(filtered);
}

function renderScopeTabs() {
  const t = getCopy();
  const activeCount = state.docs.filter(doc => !doc.is_deleted).length;
  const trashCount = state.docs.filter(doc => !!doc.is_deleted).length;
  ui.scopeTabs.innerHTML = [
    { key: "active", label: formatText(t.activeTab, { count: activeCount }) },
    { key: "trash", label: formatText(t.trashTab, { count: trashCount }) }
  ].map(tab => {
    const active = state.currentScope === tab.key ? " active" : "";
    return `<button class="scope-btn${active}" type="button" data-scope="${escapeAttr(tab.key)}">${escapeHtml(tab.label)}</button>`;
  }).join("");
  ui.docSummary.textContent = state.currentScope === "trash"
    ? formatText(t.documentsInTrash, { count: trashCount, suffix: trashCount === 1 ? "" : t.suffixPlural })
    : formatText(t.activeDocuments, { count: activeCount, suffix: activeCount === 1 ? "" : t.suffixPlural });
}

function buildEmptyState(message) {
  return `<div class="empty-state">${escapeHtml(message)}</div>`;
}

function buildDocCard(doc) {
  const t = getCopy();
  const previewText = extractPreviewText(doc);
  const updatedLabel = doc.is_deleted ? formatText(t.deletedAt, { date: formatDate(doc.deleted_at || doc.updated_at || doc.created_at) }) : formatText(t.editedAt, { date: formatDate(doc.updated_at || doc.created_at) });
  const badges = [
    doc.is_starred ? `<span class="doc-badge starred">${escapeHtml(t.protected)}</span>` : "",
    doc.is_deleted ? `<span class="doc-badge trash">${escapeHtml(t.trash)}</span>` : ""
  ].filter(Boolean).join("");
  const deleteDisabled = doc.is_starred ? "disabled" : "";
  const deleteTitle = doc.is_starred ? t.starredMustUnstar : t.moveToTrash;
  return `
    <article class="doc-card${doc.is_deleted ? " trash-card" : ""}">
      <button class="doc-preview" type="button" data-open-doc="${escapeAttr(doc.id)}">
        <div class="doc-preview-sheet">
          <div class="preview-copy">
            <div class="preview-icon">&#128196;</div>
            <strong>${escapeHtml(doc.title || t.untitledDocument)}</strong>
            <span>${escapeHtml(previewText)}</span>
          </div>
        </div>
      </button>
      <div class="doc-card-body">
        <div class="doc-card-top">
          <input class="doc-title-input" data-title-id="${escapeAttr(doc.id)}" value="${escapeAttr(doc.title || t.untitledDocument)}" ${doc.is_deleted ? "readonly" : ""} />
          <button class="doc-star-btn${doc.is_starred ? " is-starred" : ""}" type="button" data-toggle-star="${escapeAttr(doc.id)}" ${doc.is_deleted ? "disabled" : ""} title="${doc.is_starred ? t.unstarDocument : t.starDocument}">${doc.is_starred ? "&#9733;" : "&#9734;"}</button>
        </div>
        <div class="doc-card-meta">${escapeHtml(updatedLabel)}</div>
        <div class="doc-card-badges">${badges}</div>
        <div class="doc-card-actions">
          <button class="card-action" type="button" data-open-doc="${escapeAttr(doc.id)}">${escapeHtml(t.open)}</button>
          ${doc.is_deleted
            ? `<button class="card-action" type="button" data-restore-doc="${escapeAttr(doc.id)}">${escapeHtml(t.restore)}</button>`
            : `<button class="card-action danger" type="button" data-delete-doc="${escapeAttr(doc.id)}" ${deleteDisabled} title="${escapeAttr(deleteTitle)}">${escapeHtml(t.delete)}</button>`}
        </div>
      </div>
    </article>
  `;
}

function renderDashboard() {
  const t = getCopy();
  renderScopeTabs();
  const visibleDocs = getVisibleDocs();
  const starredDocs = sortDocs(state.docs.filter(doc => !doc.is_deleted && doc.is_starred && String(doc.title || "").toLowerCase().includes(state.query.trim().toLowerCase())));

  ui.userMeta.textContent = state.user
    ? text("signedInAs", { username: state.user.username, role: state.user.role ? text("roleSuffix", { role: state.user.role }) : "" })
    : t.loadingWorkspace;
  ui.searchInput.value = state.query;
  ui.starredSection.classList.toggle("hidden", state.currentScope === "trash");
  ui.clearTrashBtn.classList.toggle("hidden", state.currentScope !== "trash");
  ui.clearTrashBtn.disabled = state.saving;
  ui.allDocsHeading.textContent = state.currentScope === "trash" ? t.trash : t.allDocuments;
  ui.allDocsMeta.textContent = state.currentScope === "trash" ? t.trashDescription : t.mostRecentFirst;

  if (state.currentScope !== "trash") {
    ui.starredDocs.innerHTML = starredDocs.length
      ? starredDocs.map(buildDocCard).join("")
      : buildEmptyState(t.noStarredDocs);
  }

  ui.allDocsGrid.innerHTML = visibleDocs.length
    ? visibleDocs.map(buildDocCard).join("")
    : buildEmptyState(state.currentScope === "trash" ? t.trashEmpty : t.noDocumentsFound);
}

function renderSaveState() {
  const t = getCopy();
  const doc = getCurrentDoc();
  if (!doc) {
    ui.editorSaveState.textContent = t.allChangesSaved;
    return;
  }
  if (doc.is_deleted) {
    ui.editorSaveState.textContent = t.readOnly;
    return;
  }
  if (state.saving) {
    ui.editorSaveState.textContent = t.saving;
    return;
  }
  ui.editorSaveState.textContent = state.dirty ? t.savingChanges : t.allChangesSaved;
}

function renderEditor() {
  const t = getCopy();
  const doc = getCurrentDoc();
  const inEditor = !!doc;
  ui.dashboardView.classList.toggle("hidden", inEditor);
  ui.editorView.classList.toggle("hidden", !inEditor);
  if (!doc) {
    return;
  }

  const html = String(doc.content_html || storedContentToHtml(doc.content_markdown || ""));
  const shouldReplace = state.lastLoadedDocId !== doc.id || (!state.dirty && !state.saving && ui.editorSurface.innerHTML !== html);
  if (shouldReplace) {
    ui.editorSurface.innerHTML = html;
    normalizeEditorLists();
    state.lastLoadedDocId = doc.id;
    state.lastLoadedHtml = html;
  }

  if (document.activeElement !== ui.editorTitleInput || !state.dirty) {
    ui.editorTitleInput.value = doc.title || t.untitledDocument;
  }

  const readOnly = !!doc.is_deleted;
  ui.editorView.classList.toggle("readonly", readOnly);
  ui.editorTitleInput.disabled = readOnly;
  ui.editorSurface.setAttribute("contenteditable", readOnly ? "false" : "true");
  ui.editorStarBtn.hidden = readOnly;
  ui.editorDeleteBtn.hidden = readOnly;
  ui.editorRestoreBtn.hidden = !readOnly;
  ui.trashBanner.classList.toggle("hidden", !readOnly);

  ui.editorStarBtn.textContent = doc.is_starred ? t.starredButton : t.star;
  ui.editorStarBtn.classList.toggle("is-starred", !!doc.is_starred);
  ui.editorStarBtn.disabled = state.saving;
  ui.editorDeleteBtn.disabled = state.saving || doc.is_starred;
  ui.editorDeleteBtn.title = doc.is_starred ? t.starredMustUnstar : t.moveToTrash;
  ui.editorRestoreBtn.disabled = state.saving;

  ui.editorMeta.textContent = readOnly
    ? formatText(t.deletedAt, { date: formatDate(doc.deleted_at || doc.updated_at || doc.created_at) })
    : `${formatText(t.editedAt, { date: formatDate(doc.updated_at || doc.created_at) })}${doc.created_at ? ` | ${formatText(t.createdAt, { date: formatDate(doc.created_at) })}` : ""}`;

  [ui.fontSelect, ui.fontSizeSelect, ui.textColorInput, ui.highlightColorInput, ui.clearHighlightBtn, ui.checklistBtn, ...document.querySelectorAll("[data-command]"), ...document.querySelectorAll("[data-list]")].forEach(control => {
    control.disabled = readOnly || state.saving;
  });

  renderSaveState();
}

function renderAll() {
  renderDashboard();
  renderEditor();
}

function markDirty() {
  const doc = getCurrentDoc();
  if (!doc || doc.is_deleted) {
    return;
  }
  state.dirty = true;
  renderSaveState();
  window.clearTimeout(state.saveTimer);
  state.saveTimer = window.setTimeout(() => {
    saveCurrentDocument().catch(() => {});
  }, 350);
}

async function saveCurrentDocument() {
  const t = getCopy();
  const doc = getCurrentDoc();
  if (!doc || doc.is_deleted || state.saving) {
    return true;
  }
  const title = (ui.editorTitleInput.value || "").trim() || t.untitledDocument;
  const contentMarkdown = editorHtmlToStoredContent();
  if (!state.dirty && title === (doc.title || t.untitledDocument) && contentMarkdown === (doc.content_markdown || doc.content_html || "")) {
    return true;
  }

  state.saving = true;
  renderSaveState();
  try {
    const payload = await api(`/docs/${encodeURIComponent(doc.id)}`, "PATCH", {
      title,
      type: doc.type || "markdown",
      content_markdown: contentMarkdown
    });
    const updated = payload.document || {};
    const changedDuringSave = ((ui.editorTitleInput.value || "").trim() || t.untitledDocument) !== title || editorHtmlToStoredContent() !== contentMarkdown;
    mergeDoc(doc.id, {
      ...updated,
      title,
      content_markdown: contentMarkdown,
      content_html: contentMarkdown,
      is_deleted: false
    });
    state.dirty = changedDuringSave;
    state.lastLoadedDocId = doc.id;
    state.lastLoadedHtml = contentMarkdown;
    setWarning((payload.warnings || []).map(formatStorageWarning).filter(Boolean).join(" | "));
    setStatus(changedDuringSave ? t.continuingSave : t.saved, "ok");
  } catch (error) {
    setStatus(error.message || t.failedSave, "err");
    return false;
  } finally {
    state.saving = false;
    renderSaveState();
    renderDashboard();
  }

  if (state.dirty) {
    markDirty();
  }
  return true;
}

async function loadSession() {
  const payload = await api("/auth/me");
  state.user = payload.user || null;
  if (state.user) {
    syncPreferencesFromUser(state.user);
  } else {
    renderStaticText();
  }
}

async function loadDocs() {
  const payload = await api("/docs?include_deleted=true");
  const nextDocs = Array.isArray(payload.documents) ? payload.documents.map(doc => ({ ...doc })) : [];
  const byId = new Map(state.docs.map(doc => [String(doc.id), doc]));
  state.docs = nextDocs.map(doc => {
    const existing = byId.get(String(doc.id));
    return existing ? { ...existing, ...doc } : doc;
  });
  setWarning(formatStorageWarning(payload.warning));
}

async function openDoc(docId) {
  const t = getCopy();
  const current = getCurrentDoc();
  if (current && String(current.id) !== String(docId)) {
    const saved = await saveCurrentDocument();
    if (!saved) {
      return;
    }
  }
  state.loadingDoc = true;
  setStatus(t.loadingDocument);
  try {
    const row = getDoc(docId);
    const query = row && row.is_deleted ? "?include_deleted=true" : "";
    const payload = await api(`/docs/${encodeURIComponent(docId)}${query}`);
    const incoming = payload.document || {};
    mergeDoc(docId, {
      ...incoming,
      content_markdown: incoming.content_markdown || "",
      content_html: storedContentToHtml(incoming.content_markdown || "")
    });
    state.currentDocId = docId;
    state.dirty = false;
    setStatus(incoming.is_deleted ? t.openedTrashDocument : t.documentReady, incoming.is_deleted ? "" : "ok");
    renderAll();
  } catch (error) {
    setStatus(error.message || t.failedLoadDocument, "err");
  } finally {
    state.loadingDoc = false;
  }
}

async function backToDashboard() {
  const t = getCopy();
  const saved = await saveCurrentDocument();
  if (!saved) {
    return;
  }
  state.currentDocId = null;
  state.dirty = false;
  renderAll();
  setStatus(t.backToDashboard, "ok");
}

async function createDoc() {
  const t = getCopy();
  const saved = await saveCurrentDocument();
  if (!saved) {
    return;
  }
  setStatus(t.creatingDocument);
  try {
    const payload = await api("/docs", "POST", {
      title: t.untitledDocument,
      type: "markdown",
      content_markdown: "<div><br></div>"
    });
    const created = payload.document || null;
    if (!created || !created.id) {
      throw new Error(t.documentCreationMissingId);
    }
    state.docs.unshift({
      ...created,
      title: created.title || t.untitledDocument,
      type: created.type || "markdown",
      created_at: created.created_at || created.updated_at || new Date().toISOString(),
      updated_at: created.updated_at || new Date().toISOString(),
      content_markdown: "<div><br></div>",
      content_html: "<div><br></div>",
      is_deleted: false,
      is_starred: !!created.is_starred
    });
    setWarning((payload.warnings || []).map(formatStorageWarning).filter(Boolean).join(" | "));
    await openDoc(created.id);
  } catch (error) {
    setStatus(error.message || t.failedCreateDocument, "err");
  }
}

async function renameDocument(docId, value) {
  const t = getCopy();
  const nextTitle = String(value || "").trim() || t.untitledDocument;
  const doc = getDoc(docId);
  if (!doc || doc.is_deleted || nextTitle === (doc.title || t.untitledDocument)) {
    return;
  }
  mergeDoc(docId, { title: nextTitle, updated_at: new Date().toISOString() });
  renderDashboard();
  if (String(state.currentDocId) === String(docId) && document.activeElement !== ui.editorTitleInput) {
    ui.editorTitleInput.value = nextTitle;
  }
  try {
    const payload = await api(`/docs/${encodeURIComponent(docId)}`, "PATCH", { title: nextTitle });
    const updated = payload.document || {};
    mergeDoc(docId, { ...updated, title: nextTitle });
    setWarning((payload.warnings || []).map(formatStorageWarning).filter(Boolean).join(" | "));
    setStatus(t.titleUpdated, "ok");
    renderDashboard();
    renderEditor();
  } catch (error) {
    setStatus(error.message || t.failedRenameDocument, "err");
    mergeDoc(docId, { title: doc.title, updated_at: doc.updated_at });
    renderAll();
  }
}

async function toggleStar(docId) {
  const t = getCopy();
  const doc = getDoc(docId);
  if (!doc || doc.is_deleted || state.saving) {
    return;
  }
  const nextStarred = !doc.is_starred;
  try {
    await api(`/docs/${encodeURIComponent(docId)}/star`, "POST", { starred: nextStarred });
    mergeDoc(docId, { is_starred: nextStarred, updated_at: new Date().toISOString() });
    setStatus(nextStarred ? t.documentStarredProtected : t.documentUnstarred, "ok");
    renderAll();
  } catch (error) {
    setStatus(error.message || t.failedUpdateStar, "err");
  }
}

async function deleteDocument(docId) {
  const t = getCopy();
  const doc = getDoc(docId);
  if (!doc || doc.is_deleted) {
    return;
  }
  if (doc.is_starred) {
    setStatus(t.starredMustUnstar, "err");
    return;
  }
  if (String(state.currentDocId) === String(docId) && state.dirty) {
    const saved = await saveCurrentDocument();
    if (!saved) {
      return;
    }
  }
  try {
    await api(`/docs/${encodeURIComponent(docId)}`, "DELETE");
    mergeDoc(docId, {
      is_deleted: true,
      deleted_at: new Date().toISOString(),
      updated_at: new Date().toISOString()
    });
    state.currentScope = "trash";
    if (String(state.currentDocId) === String(docId)) {
      state.currentDocId = null;
      state.dirty = false;
    }
    renderAll();
    setStatus(t.documentMovedToTrash, "ok");
  } catch (error) {
    setStatus(error.message || t.failedDeleteDocument, "err");
  }
}

async function restoreDocument(docId) {
  const t = getCopy();
  const doc = getDoc(docId);
  if (!doc || !doc.is_deleted) {
    return;
  }
  try {
    await api(`/docs/${encodeURIComponent(docId)}/restore`, "POST");
    mergeDoc(docId, {
      is_deleted: false,
      deleted_at: null,
      updated_at: new Date().toISOString()
    });
    state.currentScope = "active";
    renderAll();
    if (String(state.currentDocId) === String(docId)) {
      setStatus(t.documentRestored, "ok");
    } else {
      setStatus(t.documentRestoredActive, "ok");
    }
  } catch (error) {
    setStatus(error.message || t.failedRestoreDocument, "err");
  }
}

async function clearTrash() {
  const t = getCopy();
  const trashedDocs = state.docs.filter(doc => !!doc.is_deleted);
  if (!trashedDocs.length) {
    setStatus(t.trashAlreadyEmpty, "ok");
    return;
  }
  if (!window.confirm(t.clearTrashConfirm)) {
    return;
  }
  try {
    const payload = await api("/docs/trash/clear", "POST");
    state.docs = state.docs.filter(doc => !doc.is_deleted);
    if (state.currentScope === "trash") {
      state.currentScope = "active";
    }
    if (getCurrentDoc() && getCurrentDoc().is_deleted) {
      state.currentDocId = null;
      state.dirty = false;
    }
    setWarning((payload.warnings || []).map(formatStorageWarning).filter(Boolean).join(" | "));
    renderAll();
    setStatus(payload.deleted_count ? t.trashCleared : t.trashAlreadyEmpty, "ok");
  } catch (error) {
    setStatus(error.message || t.failedClearTrash, "err");
  }
}

function runCommand(command, value = null) {
  const doc = getCurrentDoc();
  if (!doc || doc.is_deleted) {
    return;
  }
  ui.editorSurface.focus();
  document.execCommand("styleWithCSS", false, true);
  document.execCommand(command, false, value);
  normalizeEditorLists();
  markDirty();
}

function toggleList(type) {
  const command = type === "ul" ? "insertUnorderedList" : "insertOrderedList";
  runCommand(command);
}

function clearHighlight() {
  const doc = getCurrentDoc();
  if (!doc || doc.is_deleted) {
    return;
  }
  ui.editorSurface.focus();
  document.execCommand("hiliteColor", false, "transparent");
  document.execCommand("backColor", false, "transparent");
  markDirty();
}

function insertChecklist() {
  const t = getCopy();
  const doc = getCurrentDoc();
  if (!doc || doc.is_deleted) {
    return;
  }
  ui.editorSurface.focus();
  document.execCommand("insertHTML", false, `<ul data-type="checklist"><li><label><input type="checkbox" /> <span>${escapeHtml(t.checklistItem)}</span></label></li></ul>`);
  normalizeEditorLists();
  markDirty();
}

function insertEditorIndentation() {
  const selection = window.getSelection();
  if (!selection || !selection.rangeCount) {
    return false;
  }
  const range = selection.getRangeAt(0);
  range.deleteContents();
  const node = document.createTextNode("\u00A0\u00A0\u00A0\u00A0");
  range.insertNode(node);
  range.setStartAfter(node);
  range.collapse(true);
  selection.removeAllRanges();
  selection.addRange(range);
  ui.editorSurface.focus();
  return true;
}

async function handleEditorPaste(event) {
  const doc = getCurrentDoc();
  if (!doc || doc.is_deleted) return;
  const t = getCopy();
  const now = Date.now();

  // 1. 5-second cooldown between pastes
  if (now - state.lastPasteTime < 5000) {
    event.preventDefault();
    setStatus(t.pasteCooldown, "warn");
    try {
      await api("/docs/report-paste-abuse", "POST", {
        doc_id: state.currentDocId,
        abuse_type: "paste_cooldown",
        detail: "Paste within 5s cooldown"
      });
    } catch {}
    return;
  }

  const pastedText = event.clipboardData ? event.clipboardData.getData("text/plain") : "";

  // 2. Character length check (7500 total chars)
  if (pastedText.length > 7500) {
    event.preventDefault();
    setStatus(t.pasteTooLong.replace("{count}", pastedText.length), "warn");
    try {
      await api("/docs/report-paste-abuse", "POST", {
        doc_id: state.currentDocId,
        abuse_type: "paste_too_long",
        detail: `Pasted ${pastedText.length} chars`
      });
    } catch {}
    return;
  }

  // 3. Duplicate content check (block after 6 pastes of same content)
  if (pastedText.trim().length > 0) {
    const normalized = pastedText.trim().toLowerCase().replace(/\s+/g, " ");
    if (Object.keys(state.pasteContentCounts).length > 100) {
      state.pasteContentCounts = {};
    }
    const prev = state.pasteContentCounts[normalized] || 0;
    const next = prev + 1;
    state.pasteContentCounts[normalized] = next;
    if (next >= 6) {
      event.preventDefault();
      setStatus(t.pasteDuplicate, "warn");
      try {
        await api("/docs/report-paste-abuse", "POST", {
          doc_id: state.currentDocId,
          abuse_type: "paste_duplicate",
          detail: `Duplicate paste count: ${next}`
        });
      } catch {}
      return;
    }
  }

  // All checks passed — allow paste and update timestamp
  state.lastPasteTime = now;
}

function handleEditorKeyDown(event) {
  const doc = getCurrentDoc();
  if (!doc || doc.is_deleted) {
    return;
  }
  const selection = window.getSelection();
  const anchorElement = selection && selection.anchorNode
    ? (selection.anchorNode.nodeType === Node.ELEMENT_NODE ? selection.anchorNode : selection.anchorNode.parentElement)
    : null;
  const listItem = anchorElement && typeof anchorElement.closest === "function" ? anchorElement.closest("li") : null;
  if (event.key === "Tab" && listItem) {
    event.preventDefault();
    document.execCommand(event.shiftKey ? "outdent" : "indent", false);
    normalizeEditorLists();
    markDirty();
    return;
  }
  if (event.key === "Tab") {
    event.preventDefault();
    if (!event.shiftKey) {
      insertEditorIndentation();
      markDirty();
    }
    return;
  }
  if (event.key === "Enter" && listItem) {
    window.setTimeout(() => {
      normalizeEditorLists();
      markDirty();
    }, 0);
  }
}

function handleEditorClick(event) {
  const target = event.target;
  if (target instanceof HTMLInputElement && target.type === "checkbox") {
    markDirty();
  }
}

async function logout() {
  await saveCurrentDocument();
  try {
    await api("/auth/logout", "POST");
  } catch {
    // Ignore logout failures and still return to the home page.
  }
  window.location.href = "/";
}

function bindEvents() {
  ui.searchInput.addEventListener("input", event => {
    state.query = event.target.value || "";
    renderDashboard();
  });

  ui.newDocBtn.addEventListener("click", () => {
    createDoc().catch(() => {});
  });

  ui.allDocsNewBtn.addEventListener("click", () => {
    createDoc().catch(() => {});
  });

  ui.clearTrashBtn.addEventListener("click", () => {
    clearTrash().catch(() => {});
  });

  ui.scopeTabs.addEventListener("click", event => {
    const button = event.target.closest("[data-scope]");
    if (!button) {
      return;
    }
    state.currentScope = button.getAttribute("data-scope") || "active";
    renderDashboard();
  });

  ui.dashboardView.addEventListener("click", event => {
    const openButton = event.target.closest("[data-open-doc]");
    if (openButton) {
      openDoc(openButton.getAttribute("data-open-doc")).catch(() => {});
      return;
    }
    const starButton = event.target.closest("[data-toggle-star]");
    if (starButton) {
      toggleStar(starButton.getAttribute("data-toggle-star")).catch(() => {});
      return;
    }
    const deleteButton = event.target.closest("[data-delete-doc]");
    if (deleteButton) {
      deleteDocument(deleteButton.getAttribute("data-delete-doc")).catch(() => {});
      return;
    }
    const restoreButton = event.target.closest("[data-restore-doc]");
    if (restoreButton) {
      restoreDocument(restoreButton.getAttribute("data-restore-doc")).catch(() => {});
    }
  });

  ui.dashboardView.addEventListener("change", event => {
    const input = event.target.closest("[data-title-id]");
    if (!input) {
      return;
    }
    renameDocument(input.getAttribute("data-title-id"), input.value).catch(() => {});
  });

  ui.dashboardView.addEventListener("keydown", event => {
    const input = event.target.closest("[data-title-id]");
    if (input && event.key === "Enter") {
      event.preventDefault();
      input.blur();
    }
  });

  ui.backBtn.addEventListener("click", () => {
    backToDashboard().catch(() => {});
  });

  ui.editorTitleInput.addEventListener("input", () => {
    markDirty();
  });

  ui.editorStarBtn.addEventListener("click", () => {
    const doc = getCurrentDoc();
    if (doc) {
      toggleStar(doc.id).catch(() => {});
    }
  });

  ui.editorDeleteBtn.addEventListener("click", () => {
    const doc = getCurrentDoc();
    if (doc) {
      deleteDocument(doc.id).catch(() => {});
    }
  });

  ui.editorRestoreBtn.addEventListener("click", () => {
    const doc = getCurrentDoc();
    if (doc) {
      restoreDocument(doc.id).catch(() => {});
    }
  });

  document.querySelectorAll("[data-command]").forEach(button => {
    button.addEventListener("click", () => {
      runCommand(button.getAttribute("data-command") || "");
    });
  });

  document.querySelectorAll("[data-list]").forEach(button => {
    button.addEventListener("click", () => {
      toggleList(button.getAttribute("data-list") || "ul");
    });
  });

  ui.fontSelect.addEventListener("change", () => {
    runCommand("fontName", ui.fontSelect.value);
  });

  ui.fontSizeSelect.addEventListener("change", () => {
    runCommand("fontSize", ui.fontSizeSelect.value);
  });

  ui.textColorInput.addEventListener("input", () => {
    runCommand("foreColor", ui.textColorInput.value);
  });

  ui.highlightColorInput.addEventListener("input", () => {
    runCommand("hiliteColor", ui.highlightColorInput.value);
  });

  ui.clearHighlightBtn.addEventListener("click", clearHighlight);
  ui.checklistBtn.addEventListener("click", insertChecklist);
  ui.editorSurface.addEventListener("input", () => {
    normalizeEditorLists();
    markDirty();
  });
  ui.editorSurface.addEventListener("keydown", handleEditorKeyDown);
  ui.editorSurface.addEventListener("click", handleEditorClick);
  ui.editorSurface.addEventListener("paste", handleEditorPaste);

  document.addEventListener("selectionchange", () => {
    normalizeEditorLists();
  });

  window.addEventListener("beforeunload", () => {
    if (state.currentDocId && state.dirty && !state.saving) {
      saveCurrentDocument().catch(() => {});
    }
  });
}

async function init() {
  bindEvents();
  loadLocalPreferences();
  renderStaticText();
  try {
    await loadSession();
    await loadDocs();
    renderStaticText();
    renderAll();
    setStatus(getCopy().ready, "ok");
  } catch (error) {
    if (error.status === 401) {
      window.location.href = "/";
      return;
    }
    setStatus(error.message || getCopy().failedLoadDocs, "err");
  }
}

init().catch(error => {
  setStatus(error.message || getCopy().failedInitDocs, "err");
});



