(() => {
  "use strict";

  const OUTPUT_KINDS = [
    { kind: "markdown", label: "MD" },
    { kind: "compat_markdown", label: "兼容 MD" },
    { kind: "tex", label: "TeX" },
    { kind: "pdf", label: "PDF" },
    { kind: "log", label: "日志" },
    { kind: "summary", label: "摘要" }
  ];

  const STATUS_LABELS = {
    queued: "排队中",
    running: "运行中",
    done: "已完成",
    needs_review: "需要复核",
    failed: "失败"
  };

  const STAGES = {
    prepare: "准备",
    ocr: "MinerU OCR",
    merge: "合并 Markdown",
    repair: "结构修复",
    proofread: "LLM 校对",
    pandoc: "生成 LaTeX",
    validate: "XeLaTeX 编译",
    llm_repair: "LLM 修复编译错误",
    finalize: "整理产物"
  };

  const noticeRegion = document.getElementById("notice-region");
  const settingsForm = document.getElementById("settings-form");
  const mineruToken = document.getElementById("mineru-token");
  const mineruTokenState = document.getElementById("mineru-token-state");
  const llmApiKey = document.getElementById("llm-api-key");
  const llmApiKeyState = document.getElementById("llm-api-key-state");
  const llmBaseUrl = document.getElementById("llm-base-url");
  const llmModel = document.getElementById("llm-model");

  const jobForm = document.getElementById("job-form");
  const pdfFile = document.getElementById("pdf-file");
  const fileName = document.getElementById("file-name");
  const fileSize = document.getElementById("file-size");
  const submitHint = document.getElementById("submit-hint");
  const submitJob = document.getElementById("submit-job");
  const language = document.getElementById("language");
  const proofread = document.getElementById("proofread");
  const llmRepair = document.getElementById("llm-repair");
  const repairRounds = document.getElementById("repair-rounds");
  const chunkSize = document.getElementById("chunk-size");
  const cjkFont = document.getElementById("cjk-font");
  const mainFont = document.getElementById("main-font");
  const monoFont = document.getElementById("mono-font");
  const mathFont = document.getElementById("math-font");
  const markdownOnly = document.getElementById("markdown-only");

  const refreshJobsButton = document.getElementById("refresh-jobs");
  const jobStatus = document.getElementById("job-status");
  const jobStageLabel = document.getElementById("job-stage-label");
  const jobFilename = document.getElementById("job-filename");
  const jobUpdated = document.getElementById("job-updated");
  const progress = document.querySelector(".progress");
  const progressBar = document.getElementById("job-progress-bar");
  const jobMessage = document.getElementById("job-message");
  const jobDiagnostics = document.getElementById("job-diagnostics");
  const retryJobButton = document.getElementById("retry-job");
  const downloadButtons = document.getElementById("download-buttons");
  const historyList = document.getElementById("history-list");

  const logOutput = document.getElementById("log-output");
  const logPauseButton = document.getElementById("log-pause");
  const logClearButton = document.getElementById("log-clear");

  const state = {
    mineruConfigured: false,
    submitting: false,
    selectedJobId: null,
    sourceJobId: null,
    source: null,
    sourceManualClose: false,
    reconnectTimer: null,
    detailTimer: null,
    jobs: [],
    logPaused: false,
    logJobId: null,
    pendingLogLines: [],
    maxLogLines: 5000
  };

  function text(node, value) {
    if (node) node.textContent = value;
  }

  function formatBytes(bytes) {
    if (!Number.isFinite(bytes) || bytes < 0) return "";
    if (bytes < 1024) return `${bytes} B`;
    const units = ["KB", "MB", "GB"];
    let value = bytes / 1024;
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    return `${value >= 10 ? Math.round(value) : value.toFixed(1)} ${units[index]}`;
  }

  function formatTime(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString("zh-CN", { hour12: false });
  }

  function detailText(detail) {
    if (detail == null) return "";
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail.map(item => detailText(item)).filter(Boolean).join("；");
    }
    if (typeof detail === "object") {
      const message = detail.msg || detail.message || detail.detail || detail.error;
      if (message) return detailText(message);
      const location = [detail.loc, detail.field].filter(Boolean).flat().join(".");
      return [location, detailText(detail.msg || detail.message)].filter(Boolean).join(": ");
    }
    return String(detail);
  }

  function readPayload(response) {
    return response.text().then(rawText => {
      if (!rawText) return null;
      try {
        return JSON.parse(rawText);
      } catch (error) {
        return rawText;
      }
    });
  }

  async function request(path, options = {}) {
    let response;
    try {
      response = await fetch(path, options);
    } catch (error) {
      throw new Error("网络请求失败，请确认后端服务正在运行。");
    }
    const payload = await readPayload(response);
    if (!response.ok) {
      const detail = payload && typeof payload === "object" && !Array.isArray(payload)
        ? detailText(payload.detail)
        : detailText(payload);
      throw new Error(detail || `请求失败（HTTP ${response.status}）`);
    }
    return payload;
  }

  function showNotice(message, tone = "error", autoHide = tone === "success" || tone === "info") {
    if (!noticeRegion) return;
    if (!message) return;
    const item = document.createElement("div");
    item.className = `notice ${tone}`;
    item.textContent = message;
    noticeRegion.append(item);
    if (autoHide) {
      window.setTimeout(() => item.remove(), 6000);
    }
  }

  function setSecretField(input, hint, info) {
    if (!input || !hint) return;
    const set = Boolean(info && info.set);
    const mask = info && info.mask ? info.mask : "";
    text(hint, set ? `已保存掩码：${mask}` : "未保存");
  }

  function populateSettings(payload) {
    if (!payload) return;
    state.mineruConfigured = Boolean(payload.mineru_token && payload.mineru_token.set);
    setSecretField(mineruToken, mineruTokenState, payload.mineru_token);
    setSecretField(llmApiKey, llmApiKeyState, payload.llm && payload.llm.api_key);
    if (payload.llm) {
      if (llmBaseUrl) llmBaseUrl.value = payload.llm.base_url || "";
      if (llmModel) llmModel.value = payload.llm.model || "";
    }
    populateDefaults(payload.defaults);
    updateSubmitState();
  }

  function populateDefaults(defaults) {
    if (!defaults) return;
    if (defaults.language && language) {
      language.value = defaults.language;
    }
    if (proofread) proofread.checked = Boolean(defaults.proofread);
    if (llmRepair) llmRepair.checked = Boolean(defaults.llm_repair);
    if (repairRounds && defaults.repair_rounds != null) repairRounds.value = defaults.repair_rounds;
    if (chunkSize && defaults.chunk_size != null) chunkSize.value = defaults.chunk_size;
    if (cjkFont && defaults.cjk_font) cjkFont.value = defaults.cjk_font;
    if (mainFont && defaults.main_font) mainFont.value = defaults.main_font;
    if (monoFont && defaults.mono_font) monoFont.value = defaults.mono_font;
    if (mathFont && defaults.math_font) mathFont.value = defaults.math_font;
  }

  function updateSubmitState() {
    const file = pdfFile && pdfFile.files && pdfFile.files[0];
    const reasons = [];
    if (!file) reasons.push("请先选择 PDF 文件");
    if (!state.mineruConfigured) reasons.push("请先保存 MinerU token");
    if (submitHint) {
      text(submitHint, reasons.join("；"));
      submitHint.classList.toggle("warning", reasons.length > 0);
    }
    if (submitJob) submitJob.disabled = reasons.length > 0 || state.submitting;
  }

  function updateFileMeta() {
    const file = pdfFile && pdfFile.files && pdfFile.files[0];
    if (!file) {
      text(fileName, "未选择文件");
      text(fileSize, "");
    } else {
      text(fileName, file.name);
      text(fileSize, formatBytes(file.size));
    }
    updateSubmitState();
  }

  function integerOrEmpty(input) {
    if (!input || input.value.trim() === "") return null;
    const value = Number(input.value);
    return Number.isFinite(value) ? Math.trunc(value) : null;
  }

  function optionsFromForm() {
    return {
      language: language ? language.value : "ch",
      chunk_size: integerOrEmpty(chunkSize),
      proofread: proofread ? proofread.checked : false,
      llm_repair: llmRepair ? llmRepair.checked : true,
      repair_rounds: integerOrEmpty(repairRounds),
      cjk_font: cjkFont ? cjkFont.value : "",
      main_font: mainFont ? mainFont.value : "",
      mono_font: monoFont ? monoFont.value : "",
      math_font: mathFont ? mathFont.value : "",
      no_pdf: markdownOnly ? markdownOnly.checked : false
    };
  }

  function renderCurrentJob(job) {
    if (!job) return;
    const status = job.status in STATUS_LABELS ? job.status : "queued";
    if (jobStatus) {
      jobStatus.className = `status-badge ${status}`;
      text(jobStatus, STATUS_LABELS[status]);
    }
    if (jobStageLabel) {
      const stageKey = job.stage && STAGES[job.stage] ? job.stage : "";
      text(jobStageLabel, job.stage_label || STAGES[stageKey] || "等待任务");
    }
    text(jobFilename, job.filename || "未命名任务");
    text(jobUpdated, job.updated_at ? `更新：${formatTime(job.updated_at)}` : "");

    const percent = Math.max(0, Math.min(100, Math.round((Number(job.progress) || 0) * 100)));
    if (progressBar) {
      progressBar.style.width = `${percent}%`;
      progressBar.className = `progress-bar ${status}`;
    }
    if (progress) progress.setAttribute("aria-valuenow", String(percent));
    text(jobMessage, job.message || "暂无任务消息。");

    const diagnostics = [];
    if (Array.isArray(job.warnings) && job.warnings.length) {
      diagnostics.push(`警告：\n${job.warnings.map(item => `- ${item}`).join("\n")}`);
    }
    if (Array.isArray(job.errors) && job.errors.length) {
      diagnostics.push(`错误：\n${job.errors.map(item => `- ${item}`).join("\n")}`);
    }
    if (jobDiagnostics) {
      jobDiagnostics.textContent = diagnostics.join("\n\n");
      jobDiagnostics.hidden = diagnostics.length === 0;
      jobDiagnostics.classList.toggle("has-errors", Array.isArray(job.errors) && job.errors.length > 0);
    }

    renderOutputs(job);
    if (retryJobButton) {
      retryJobButton.hidden = !(job.status === "failed" || job.status === "needs_review");
      retryJobButton.disabled = false;
    }
  }

  function renderOutputs(job) {
    if (!downloadButtons) return;
    downloadButtons.textContent = "";
    const outputs = job && job.outputs ? job.outputs : {};
    for (const item of OUTPUT_KINDS) {
      const hasOutput = Boolean(outputs[item.kind]);
      const link = document.createElement("a");
      link.className = `download-link${hasOutput ? "" : " disabled"}`;
      link.textContent = item.label;
      if (hasOutput) {
        link.href = `/api/jobs/${encodeURIComponent(job.id)}/download/${encodeURIComponent(item.kind)}`;
        link.setAttribute("download", "");
      } else {
        link.setAttribute("aria-disabled", "true");
        link.tabIndex = -1;
      }
      downloadButtons.append(link);
    }
  }

  function renderHistory(jobs) {
    if (!historyList) return;
    historyList.textContent = "";
    if (!Array.isArray(jobs) || jobs.length === 0) {
      const empty = document.createElement("li");
      empty.className = "empty-history";
      text(empty, "暂无历史任务。");
      historyList.append(empty);
      return;
    }

    for (const job of jobs) {
      const item = document.createElement("li");
      item.className = "history-item";

      const openButton = document.createElement("button");
      openButton.type = "button";
      openButton.className = "history-open";
      openButton.setAttribute("aria-label", `查看任务 ${job.filename || job.id}`);

      const title = document.createElement("span");
      title.className = "history-title";
      const name = document.createElement("span");
      name.className = "history-name";
      text(name, job.filename || "未命名任务");
      const status = document.createElement("span");
      status.className = `status-badge ${job.status in STATUS_LABELS ? job.status : "queued"}`;
      text(status, STATUS_LABELS[job.status] || "未知状态");
      title.append(name, status);

      const time = document.createElement("span");
      time.className = "history-time";
      text(time, formatTime(job.created_at));
      openButton.append(title, time);
      openButton.addEventListener("click", () => {
        void selectJob(job.id);
      });

      item.append(openButton);
      historyList.append(item);
    }
  }

  function clearLog() {
    if (logOutput) logOutput.textContent = "";
    state.pendingLogLines = [];
    if (logPauseButton) {
      text(logPauseButton, "暂停");
      logPauseButton.setAttribute("aria-pressed", "false");
    }
    state.logPaused = false;
  }

  function appendLogLine(line) {
    if (typeof line === "undefined" || line === null) return;
    const value = String(line);
    if (state.logPaused) {
      state.pendingLogLines.push(value);
      if (state.pendingLogLines.length > state.maxLogLines) {
        state.pendingLogLines.splice(0, state.pendingLogLines.length - state.maxLogLines);
      }
      if (logPauseButton) text(logPauseButton, `恢复（${state.pendingLogLines.length}）`);
      return;
    }

    if (logOutput) {
      const lines = logOutput.textContent.split("\n");
      lines.push(value);
      if (lines.length > state.maxLogLines) lines.splice(0, lines.length - state.maxLogLines);
      logOutput.textContent = lines.join("\n");
      logOutput.scrollTop = logOutput.scrollHeight;
    }
  }

  function flushLogLines() {
    if (!logOutput) return;
    if (state.pendingLogLines.length) {
      const lines = logOutput.textContent.split("\n");
      lines.push(...state.pendingLogLines);
      if (lines.length > state.maxLogLines) lines.splice(0, lines.length - state.maxLogLines);
      logOutput.textContent = lines.join("\n");
      state.pendingLogLines = [];
      logOutput.scrollTop = logOutput.scrollHeight;
    }
    text(logPauseButton, "暂停");
    logPauseButton.setAttribute("aria-pressed", "false");
  }

  function closeEventSource() {
    if (!state.source) return;
    state.sourceManualClose = true;
    state.source.close();
    state.source = null;
    state.sourceJobId = null;
    window.setTimeout(() => {
      state.sourceManualClose = false;
    }, 0);
  }

  function scheduleDetailRefresh(jobId) {
    if (state.detailTimer) return;
    state.detailTimer = window.setTimeout(async () => {
      state.detailTimer = null;
      if (!jobId || jobId !== state.selectedJobId) return;
      try {
        const job = await request(`/api/jobs/${encodeURIComponent(jobId)}`);
        renderCurrentJob(job);
        if (!job.terminal) {
          scheduleReconnect(jobId);
        }
      } catch (error) {
        showNotice(error.message);
      }
    }, 1000);
  }

  function scheduleReconnect(jobId) {
    if (state.reconnectTimer) return;
    state.reconnectTimer = window.setTimeout(() => {
      state.reconnectTimer = null;
      if (jobId && jobId === state.selectedJobId) {
        void selectJob(jobId, { forceSubscribe: true });
      }
    }, 1500);
  }

  function handleSourceMessage(event) {
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch (error) {
      appendLogLine("[前端] 收到无法解析的 SSE 数据");
      return;
    }

    if (payload.type === "log") {
      appendLogLine(payload.line);
      return;
    }
    if (payload.type === "status" && payload.job) {
      renderCurrentJob(payload.job);
      if (payload.job.terminal) {
        closeEventSource();
        void refreshJobs();
      }
    }
  }

  function subscribeJob(job) {
    if (!job || job.terminal) {
      closeEventSource();
      return;
    }

    if (state.source && state.sourceJobId === job.id) return;
    closeEventSource();

    const source = new EventSource(`/api/jobs/${encodeURIComponent(job.id)}/events`);
    state.source = source;
    state.sourceJobId = job.id;
    source.onmessage = handleSourceMessage;
    source.onerror = () => {
      if (state.sourceManualClose) return;
      scheduleDetailRefresh(job.id);
      if (source.readyState === EventSource.CLOSED) {
        scheduleReconnect(job.id);
      }
    };
  }

  async function selectJob(jobId, options = {}) {
    if (!jobId) return;
    if (options.forceSubscribe || state.selectedJobId !== jobId) {
      state.selectedJobId = jobId;
      if (state.logJobId !== jobId) clearLog();
      state.logJobId = jobId;
    }
    try {
      const job = await request(`/api/jobs/${encodeURIComponent(jobId)}`);
      renderCurrentJob(job);
      if (options.forceSubscribe || !job.terminal) {
        subscribeJob(job);
      } else {
        closeEventSource();
      }
    } catch (error) {
      showNotice(error.message);
    }
  }

  async function refreshJobs() {
    try {
      const payload = await request("/api/jobs");
      const jobs = payload && Array.isArray(payload.jobs) ? payload.jobs : [];
      state.jobs = jobs;
      renderHistory(jobs);

      if (!state.selectedJobId) {
        const active = jobs.find(job => job.status === "running" || job.status === "queued");
        const latest = jobs[0];
        const initial = active || latest;
        if (initial) {
          await selectJob(initial.id, { forceSubscribe: true });
        }
      }
      return jobs;
    } catch (error) {
      showNotice(error.message);
      return [];
    }
  }

  async function initHealth() {
    try {
      const health = await request("/api/health");
      const tools = health && health.tools ? health.tools : {};
      const missing = [];
      if (tools.pandoc === false) missing.push("pandoc");
      if (tools.xelatex === false) missing.push("xelatex");
      if (missing.length) {
        showNotice(`缺少必需工具：${missing.join("、")}。请先安装后刷新页面。`, "warning", false);
      }
    } catch (error) {
      showNotice(error.message);
    }
  }

  async function initSettings() {
    try {
      const settings = await request("/api/settings");
      populateSettings(settings);
    } catch (error) {
      showNotice(error.message);
      updateSubmitState();
    }
  }

  function clearPasswordFields() {
    if (mineruToken) mineruToken.value = "";
    if (llmApiKey) llmApiKey.value = "";
  }

  async function saveSettings(event) {
    event.preventDefault();
    const env = {};
    if (mineruToken && mineruToken.value.trim()) env.MINERU_API_TOKEN = mineruToken.value.trim();
    if (llmApiKey && llmApiKey.value.trim()) env.LLM_API_KEY = llmApiKey.value.trim();
    if (llmBaseUrl && llmBaseUrl.value.trim()) env.LLM_BASE_URL = llmBaseUrl.value.trim();
    if (llmModel && llmModel.value.trim()) env.LLM_MODEL = llmModel.value.trim();

    if (Object.keys(env).length === 0) {
      showNotice("没有填写需要保存的密钥或连接信息。", "info");
      return;
    }

    try {
      const saved = await request("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ env })
      });
      populateSettings(saved);
      clearPasswordFields();
      showNotice("设置已保存到 .env。", "success");
      updateSubmitState();
    } catch (error) {
      showNotice(error.message);
    }
  }

  async function createJob(event) {
    event.preventDefault();
    const file = pdfFile && pdfFile.files && pdfFile.files[0];
    if (!file || !state.mineruConfigured || state.submitting) {
      updateSubmitState();
      return;
    }

    state.submitting = true;
    updateSubmitState();
    if (submitJob) text(submitJob, "正在提交...");
    try {
      const formData = new FormData();
      formData.append("file", file, file.name);
      formData.append("options", JSON.stringify(optionsFromForm()));
      const job = await request("/api/jobs", { method: "POST", body: formData });
      pdfFile.value = "";
      updateFileMeta();
      await refreshJobs();
      await selectJob(job.id, { forceSubscribe: true });
    } catch (error) {
      showNotice(error.message);
    } finally {
      state.submitting = false;
      if (submitJob) text(submitJob, "提交转换任务");
      updateSubmitState();
    }
  }

  async function retryJob() {
    if (!state.selectedJobId) return;
    try {
      if (retryJobButton) retryJobButton.disabled = true;
      const job = await request(`/api/jobs/${encodeURIComponent(state.selectedJobId)}/retry`, {
        method: "POST"
      });
      await refreshJobs();
      await selectJob(job.id, { forceSubscribe: true });
    } catch (error) {
      showNotice(error.message);
      if (retryJobButton) retryJobButton.disabled = false;
    }
  }

  function bindEvents() {
    if (settingsForm) settingsForm.addEventListener("submit", saveSettings);
    if (jobForm) jobForm.addEventListener("submit", createJob);
    if (pdfFile) pdfFile.addEventListener("change", updateFileMeta);
    if (refreshJobsButton) refreshJobsButton.addEventListener("click", () => void refreshJobs());
    if (retryJobButton) retryJobButton.addEventListener("click", () => void retryJob());

    if (logPauseButton) {
      logPauseButton.setAttribute("aria-pressed", "false");
      logPauseButton.addEventListener("click", () => {
        state.logPaused = !state.logPaused;
        if (state.logPaused) {
          text(logPauseButton, "恢复");
          logPauseButton.setAttribute("aria-pressed", "true");
        } else {
          flushLogLines();
        }
      });
    }
    if (logClearButton) logClearButton.addEventListener("click", clearLog);
  }

  async function init() {
    bindEvents();
    clearLog();
    updateSubmitState();
    await Promise.all([initHealth(), initSettings()]);
    await refreshJobs();
  }

  void init();
})();
