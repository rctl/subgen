const mediaListEl = document.getElementById("mediaList");
const searchInput = document.getElementById("searchInput");
const statusEl = document.getElementById("status");
const rescanBtn = document.getElementById("rescan");

const modal = document.getElementById("modal");
const modalTitle = document.getElementById("modalTitle");
const closeModalBtn = document.getElementById("closeModal");
const sourceLangInput = document.getElementById("sourceLang");
const targetLangInput = document.getElementById("targetLang");
const translateTargetLangInput = document.getElementById("translateTargetLang");
const modeSelect = document.getElementById("mode");
const existingSubSelect = document.getElementById("existingSub");
const runGenerateBtn = document.getElementById("runGenerate");
const transcribeFields = document.getElementById("transcribeFields");
const translateFields = document.getElementById("translateFields");

let mediaItems = [];
let currentMedia = null;

function setStatus(message) {
  statusEl.textContent = message;
}

async function fetchMedia() {
  setStatus("Scanning media...");
  const url = new URL("api/media", window.location.origin + window.location.pathname);
  const response = await fetch(url);
  const data = await response.json();
  if (data.error) {
    setStatus(`Error: ${data.error}`);
    return;
  }
  mediaItems = data.items || [];
  setStatus(`${mediaItems.length} videos found.`);
  renderList();
}

function renderList() {
  const query = searchInput.value.toLowerCase();
  const filtered = mediaItems.filter((item) =>
    item.title.toLowerCase().includes(query)
  );

  mediaListEl.innerHTML = "";
  filtered.forEach((item) => {
    const card = document.createElement("div");
    card.className = "media-card";

    const title = document.createElement("div");
    title.className = "media-title";
    title.textContent = item.title;

    const path = document.createElement("div");
    path.className = "media-path";
    path.textContent = item.path;

    const badges = document.createElement("div");
    badges.className = "badges";

    const embeddedCount = item.embedded_subs?.length || 0;
    const sidecarCount = item.sidecar_subs?.length || 0;

    const embeddedBadge = document.createElement("div");
    embeddedBadge.className = `badge ${embeddedCount ? "active" : ""}`;
    embeddedBadge.textContent = `Embedded: ${embeddedCount}`;

    const sidecarBadge = document.createElement("div");
    sidecarBadge.className = `badge ${sidecarCount ? "active" : ""}`;
    sidecarBadge.textContent = `Sidecar: ${sidecarCount}`;

    badges.appendChild(embeddedBadge);
    badges.appendChild(sidecarBadge);

    const button = document.createElement("button");
    button.textContent = "Generate Subtitle";
    button.addEventListener("click", () => openModal(item));

    card.appendChild(title);
    card.appendChild(path);
    card.appendChild(badges);
    card.appendChild(button);

    mediaListEl.appendChild(card);
  });
}

function openModal(item) {
  currentMedia = item;
  modalTitle.textContent = `Generate Subtitle: ${item.title}`;
  sourceLangInput.value = sourceLangInput.value || "sv";
  targetLangInput.value = targetLangInput.value || "sv";
  translateTargetLangInput.value = translateTargetLangInput.value || "en";
  const hasSubs = (item.sidecar_subs?.length || 0) + (item.embedded_subs?.length || 0) > 0;
  modeSelect.value = hasSubs ? "translate" : "transcribe";
  populateExistingSubs(item);
  toggleModeFields();
  modal.classList.remove("hidden");
}

function populateExistingSubs(item) {
  existingSubSelect.innerHTML = "";
  const subs = [...(item.sidecar_subs || []), ...(item.embedded_subs || [])];
  if (!subs.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "No existing subtitles";
    existingSubSelect.appendChild(opt);
    return;
  }
  subs.forEach((sub) => {
    const opt = document.createElement("option");
    opt.value = sub.id;
    const label = sub.kind === "embedded" ? "Embedded" : "Sidecar";
    opt.textContent = `${label}: ${sub.lang} (${sub.title})`;
    existingSubSelect.appendChild(opt);
  });
}

async function runGenerate() {
  if (!currentMedia) return;
  setStatus("Generating subtitles...");
  let payload = { media_path: currentMedia.path };
  if (modeSelect.value === "translate") {
    payload = {
      ...payload,
      mode: "translate_existing",
      target_lang: translateTargetLangInput.value.trim(),
      existing_sub_id: existingSubSelect.value || null,
    };
  } else {
    const sourceLang = sourceLangInput.value.trim();
    const targetLang = targetLangInput.value.trim() || sourceLang;
    payload = {
      ...payload,
      mode: "transcribe",
      source_lang: sourceLang,
      target_lang: targetLang,
      existing_sub_id: null,
    };
  }

  const response = await fetch("api/subtitles/generate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (data.error) {
    setStatus(`Error: ${data.error}`);
  } else {
    setStatus(`Generated: ${data.outputs?.join(", ") || ""}`);
  }
  modal.classList.add("hidden");
  fetchMedia(mediaPathInput.value);
}

searchInput.addEventListener("input", renderList);
rescanBtn.addEventListener("click", () => fetchMedia());
closeModalBtn.addEventListener("click", () => modal.classList.add("hidden"));
runGenerateBtn.addEventListener("click", runGenerate);
modeSelect.addEventListener("change", toggleModeFields);

fetchMedia();
populateLanguageOptions();

function toggleModeFields() {
  const isTranslate = modeSelect.value === "translate";
  translateFields.classList.toggle("hidden", !isTranslate);
  transcribeFields.classList.toggle("hidden", isTranslate);
}

function populateLanguageOptions() {
  const languages = [
    { code: "en", label: "English" },
    { code: "sv", label: "Swedish" },
    { code: "zh", label: "Chinese" },
    { code: "es", label: "Spanish" },
    { code: "fr", label: "French" },
    { code: "de", label: "German" },
    { code: "it", label: "Italian" },
    { code: "pt", label: "Portuguese" },
    { code: "nl", label: "Dutch" },
    { code: "no", label: "Norwegian" },
    { code: "da", label: "Danish" },
    { code: "fi", label: "Finnish" },
    { code: "pl", label: "Polish" },
    { code: "cs", label: "Czech" },
    { code: "el", label: "Greek" },
    { code: "ja", label: "Japanese" },
    { code: "ko", label: "Korean" },
  ];

  function fill(select) {
    select.innerHTML = "";
    languages.forEach((lang) => {
      const opt = document.createElement("option");
      opt.value = lang.code;
      opt.textContent = `${lang.label} (${lang.code})`;
      select.appendChild(opt);
    });
  }

  fill(sourceLangInput);
  fill(targetLangInput);
  fill(translateTargetLangInput);
  sourceLangInput.value = "sv";
  targetLangInput.value = "sv";
  translateTargetLangInput.value = "en";
}
