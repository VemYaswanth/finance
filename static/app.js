
let currentUser = null;
let currentIsAdmin = false;
let currentSummary = null;
let currentCategories = [];
let currentScan = null;
const pieColors = ["#0f766e", "#be4b49", "#c58b22", "#4f46e5", "#7c3aed", "#0891b2", "#db2777", "#4d7c0f"];
const graphSettingsKey = "financialReviewGraphSettingsV3";
const chartDefinitions = [
  { key: "savings", panel: "savings-panel", visible: "savings-visible", type: "savings-chart-type", period: "savings-chart-period", defaultType: "line", defaultPeriod: "month" },
  { key: "income", panel: "income-panel", visible: "income-visible", type: "income-chart-type", period: "income-chart-period", defaultType: "line", defaultPeriod: "month" },
  { key: "expenses", panel: "expenses-panel", visible: "expenses-visible", type: "expenses-chart-type", period: "expenses-chart-period", defaultType: "line", defaultPeriod: "month" },
  { key: "category", panel: "category-panel", visible: "category-visible", type: "category-chart-type", period: "category-chart-period", defaultType: "pie", defaultPeriod: "all" },
  { key: "account", panel: "account-panel", visible: "account-visible", type: "account-chart-type", period: "account-chart-period", defaultType: "bar", defaultPeriod: "all" },
  { key: "merchant", panel: "merchant-panel", visible: "merchant-visible", type: "merchant-chart-type", period: "merchant-chart-period", defaultType: "bar", defaultPeriod: "all" },
];

function defaultGraphSettings() {
  return Object.fromEntries(chartDefinitions.map((chart) => [
    chart.key,
    { visible: false, type: chart.defaultType, period: chart.defaultPeriod },
  ]));
}

let graphSettings = defaultGraphSettings();

const $ = (id) => document.getElementById(id);
const money = (value) => new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(value || 0);

function applyTheme(theme) {
  const nextTheme = theme === "dark" ? "dark" : "light";
  document.documentElement.dataset.theme = nextTheme;
  localStorage.setItem("financialReviewTheme", nextTheme);
  const toggle = $("theme-toggle");
  if (toggle) toggle.textContent = nextTheme === "dark" ? "Light Mode" : "Dark Mode";
}

function initThemeToggle() {
  const savedTheme = localStorage.getItem("financialReviewTheme") || "light";
  applyTheme(savedTheme);
  const toggle = $("theme-toggle");
  if (toggle) {
    toggle.addEventListener("click", () => {
      const current = document.documentElement.dataset.theme === "dark" ? "dark" : "light";
      applyTheme(current === "dark" ? "light" : "dark");
    });
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const detail = await response.text();
    try {
      const payload = JSON.parse(detail);
      const error = new Error(payload.message || payload.code || response.statusText);
      error.code = payload.code;
      throw error;
    } catch (error) {
      if (error.code) throw error;
      throw new Error(detail || response.statusText);
    }
  }
  return response.json();
}

function setBusy(message) {
  $("subtitle").textContent = message;
}

function cacheStatusText(scan) {
  if (!scan) return "Stored transaction database ready";
  const parsed = Number(scan.parsed_statement_count || 0);
  const cached = Number(scan.cached_statement_count || 0);
  if (parsed || cached) {
    return `${cached} reused from database, ${parsed} parsed from PDF`;
  }
  return "No PDFs changed. Loaded from stored transactions.";
}

function updateCacheStatus(scan) {
  const target = $("cache-status");
  if (target) target.textContent = cacheStatusText(scan);
}

function filterQuery() {
  const params = new URLSearchParams();
  ["query", "category", "account_type", "start", "end"].forEach((id) => {
    const key = id === "query" ? "q" : id;
    if ($(id).value) params.set(key, $(id).value);
  });
  return params.toString();
}

function storeSession(session) {
  currentUser = session.user.id;
  currentIsAdmin = Boolean(session.user.is_admin);
  currentScan = null;
  localStorage.setItem("financialReviewUser", currentUser);
  if (session.session_token && $("remember-login")?.checked !== false) {
    localStorage.setItem("financialReviewSessionToken", session.session_token);
  } else if (session.session_token) {
    sessionStorage.setItem("financialReviewSessionToken", session.session_token);
    localStorage.removeItem("financialReviewSessionToken");
  }
}

async function openSession(session) {
  storeSession(session);
  $("login-screen").classList.add("hidden");
  $("workspace").classList.toggle("hidden", currentIsAdmin);
  $("admin-workspace").classList.toggle("hidden", !currentIsAdmin);
  if (currentIsAdmin) {
    await refreshAdminUsers();
    return;
  }
  $("folder-path").textContent = session.folder;
  updateCacheStatus(currentScan);
  await refreshDashboard();
}

async function loginWithUsername(form) {
  const fields = new FormData(form);
  const name = fields.get("name");
  const password = fields.get("password");
  if (!name || !String(name).trim()) return;
  if (!password) {
    $("login-status").textContent = "Enter a password. New usernames will save this as their login password.";
    return;
  }
  $("login-status").textContent = "Checking username and password...";
  const session = await api("/api/session", { method: "POST", body: fields });
  await openSession(session);
}

async function resumeSavedSession() {
  const rememberedUser = localStorage.getItem("financialReviewUser");
  const token = localStorage.getItem("financialReviewSessionToken") || sessionStorage.getItem("financialReviewSessionToken");
  if (!rememberedUser || !token) return;
  $("username").value = rememberedUser;
  $("login-status").textContent = "Restoring saved login...";
  try {
    const session = await api(`/api/session?user=${encodeURIComponent(rememberedUser)}&token=${encodeURIComponent(token)}`);
    await openSession(session);
  } catch (error) {
    localStorage.removeItem("financialReviewSessionToken");
    sessionStorage.removeItem("financialReviewSessionToken");
    $("login-status").textContent = "Saved login expired. Enter your password once to continue.";
  }
}

async function refreshDashboard() {
  if (!currentUser) return;
  const payload = await api(`/api/users/${currentUser}/summary?${filterQuery()}`);
  currentSummary = payload;
  currentIsAdmin = Boolean(payload.user.is_admin);
  if (currentIsAdmin) {
    $("workspace").classList.add("hidden");
    $("admin-workspace").classList.remove("hidden");
    await refreshAdminUsers();
    return;
  }
  $("title").textContent = payload.user.name;
  $("folder-path").textContent = payload.folder;
  $("subtitle").textContent = "Your financial view is loaded from the local database.";
  updateCacheStatus(currentScan);
  $("income").textContent = money(payload.summary.income);
  $("expenses").textContent = money(payload.summary.expenses);
  $("savings").textContent = money(payload.summary.savings);
  updateCommandCenter(payload);
  updateUserDetails(payload);
  $("summary-text").textContent = payload.summary.narrative;
  $("ai-state").textContent = payload.summary.ai_enabled ? "OpenAI summary enabled" : "Local rules summary. Set OPENAI_API_KEY for richer AI narration.";
  updatePlaidStatus(payload.plaid);
  updateCategoryOptions(payload.categories);
  updateAccountOptions(payload.account_types);
  drawManualTransactionList(payload.manual_transactions || []);
  renderCharts();
  drawTransactions(payload.transactions);
}


function updateCommandCenter(payload) {
  const transactions = (payload.transactions || []).filter((tx) => !internalCategories.has(tx.category));
  const income = Number(payload.summary?.income || 0);
  const expenses = Math.abs(Number(payload.summary?.expenses || 0));
  const savings = Number(payload.summary?.savings || 0);
  const savingsRate = income > 0 ? Math.round((savings / income) * 100) : 0;
  const score = moneyHealthScore(income, expenses, savingsRate, transactions.length);
  $("savings-rate").textContent = `${savingsRate}%`;
  $("money-health-score").textContent = transactions.length ? `${score}` : "--";
  $("money-health-label").textContent = transactions.length ? moneyHealthLabel(score) : "Waiting for data";
  $("transaction-count").textContent = String(transactions.length);
  $("activity-detail").textContent = activeFilterText();

  const categories = categoryExpenseEntries(transactions, "all", 5);
  const merchants = merchantExpenseEntries(transactions, "all", 5);
  const topCategory = categories[0];
  const topMerchant = merchants[0];
  $("top-category").textContent = topCategory ? topCategory[0] : "--";
  $("top-category-detail").textContent = topCategory ? `${money(topCategory[1])} in the current view.` : "No expense category detected yet.";
  $("top-merchant").textContent = topMerchant ? topMerchant[0] : "--";
  $("top-merchant-detail").textContent = topMerchant ? `${money(topMerchant[1])} in the current view.` : "No merchant spending detected yet.";

  const focus = suggestedFocus(income, expenses, savingsRate, topCategory, transactions.length);
  $("focus-title").textContent = focus.title;
  $("focus-text").textContent = focus.text;
  $("command-center-brief").textContent = commandCenterBrief(income, expenses, savings, savingsRate, transactions.length);
  drawBudgetWatch(categories, expenses);
}

function moneyHealthScore(income, expenses, savingsRate, count) {
  if (!count) return 0;
  let score = 55;
  if (income > 0) score += 10;
  if (savingsRate >= 20) score += 25;
  else if (savingsRate >= 10) score += 16;
  else if (savingsRate >= 0) score += 6;
  else score -= 18;
  if (expenses > income && income > 0) score -= 12;
  if (count >= 25) score += 5;
  return Math.max(1, Math.min(99, Math.round(score)));
}

function moneyHealthLabel(score) {
  if (score >= 82) return "Strong control";
  if (score >= 68) return "Stable";
  if (score >= 50) return "Watch closely";
  return "Needs attention";
}

function suggestedFocus(income, expenses, savingsRate, topCategory, count) {
  if (!count) return { title: "Import statements", text: "Add or refresh transactions to unlock financial insights." };
  if (income > 0 && expenses > income) return { title: "Reduce cash burn", text: `Expenses are higher than income by ${money(expenses - income)} in this view.` };
  if (savingsRate < 0) return { title: "Protect savings", text: "Savings are negative in this view. Review the largest categories first." };
  if (savingsRate < 10) return { title: "Improve savings rate", text: "Try targeting one high-spend category before adding new budgets." };
  if (topCategory) return { title: `Watch ${topCategory[0]}`, text: `${topCategory[0]} is currently the biggest spending category at ${money(topCategory[1])}.` };
  return { title: "Keep monitoring", text: "Your current view looks stable. Use filters to inspect specific months or accounts." };
}

function commandCenterBrief(income, expenses, savings, savingsRate, count) {
  if (!count) return "Load your statements to see cash flow, alerts, budget health, and smart next steps.";
  return `${count} transactions analyzed. Income ${money(income)}, expenses ${money(expenses)}, savings ${money(savings)}, savings rate ${savingsRate}%.`;
}

function activeFilterText() {
  const pieces = [];
  if ($("query").value) pieces.push(`matching “${$("query").value}”`);
  if ($("category").value) pieces.push(`in ${$("category").value}`);
  if ($("account_type").value) pieces.push(`${$("account_type").value} accounts`);
  if ($("start").value || $("end").value) pieces.push("date-filtered");
  return pieces.length ? `Transactions ${pieces.join(", ")}.` : "Transactions in current view.";
}

function merchantExpenseEntries(transactions, period = "all", limit = 10) {
  const rows = {};
  transactionsForWindow(transactions, period).forEach((tx) => {
    if (tx.amount < 0 && !internalCategories.has(tx.category)) {
      const merchant = merchantPatternFromDescription(tx.description) || "Unknown merchant";
      rows[merchant] = (rows[merchant] || 0) + Math.abs(tx.amount);
    }
  });
  return Object.entries(rows).sort((a, b) => b[1] - a[1]).slice(0, limit);
}

function drawBudgetWatch(entries, totalExpenses) {
  const target = $("budget-watch-list");
  if (!target) return;
  if (!entries.length || !totalExpenses) {
    target.innerHTML = `<div class="budget-empty">No spending data available for budget watch.</div>`;
    return;
  }
  target.innerHTML = entries.map(([category, amount]) => {
    const share = Math.min(100, Math.round((amount / totalExpenses) * 100));
    const level = share >= 35 ? "high" : share >= 18 ? "medium" : "normal";
    return `<div class="budget-watch-item ${level}">
      <div class="budget-watch-meta"><strong>${escapeHtml(category)}</strong><span>${share}% of spending - ${money(amount)}</span></div>
      <div class="track"><div class="budget-bar" style="width:${Math.max(4, share)}%"></div></div>
    </div>`;
  }).join("");
}

function updatePlaidStatus(plaid) {
  const status = $("plaid-status");
  const itemsTarget = $("plaid-items");
  if (!status || !itemsTarget) return;
  const items = plaid?.items || [];
  if (!plaid?.configured) {
    status.textContent = "Set PLAID_CLIENT_ID and PLAID_SECRET, then restart the app.";
  } else if (items.length) {
    status.textContent = `${items.length} Plaid item(s) connected in ${plaid.environment || "sandbox"}.`;
  } else {
    status.textContent = `Plaid is ready in ${plaid.environment || "sandbox"}.`;
  }
  itemsTarget.innerHTML = items.length ? items.map((item) => {
    const name = item.institution_name || item.item_id || "Plaid item";
    const synced = item.last_synced_at || "Not synced yet";
    const accounts = item.accounts?.length ? `${item.accounts.length} account(s)` : "Accounts load after sync";
    return `<div class="plaid-item">
      <strong>${escapeHtml(name)}</strong>
      <span>${escapeHtml(accounts)} - ${escapeHtml(synced)}</span>
    </div>`;
  }).join("") : `<div class="plaid-empty">No Plaid items connected.</div>`;
}

async function connectPlaid() {
  if (!currentUser) return;
  if (!window.Plaid) {
    $("plaid-status").textContent = "Plaid Link did not load. Check network access and reload.";
    return;
  }
  $("plaid-status").textContent = "Creating Plaid Link token...";
  try {
    const token = await api(`/api/users/${currentUser}/plaid/link-token`, { method: "POST" });
    const handler = window.Plaid.create({
      token: token.link_token,
      onSuccess: async (publicToken, metadata) => {
        $("plaid-status").textContent = "Connecting Plaid item and syncing transactions...";
        const form = new FormData();
        form.set("public_token", publicToken);
        form.set("metadata", JSON.stringify(metadata || {}));
        const result = await api(`/api/users/${currentUser}/plaid/exchange`, { method: "POST", body: form });
        $("plaid-status").textContent = `Connected. Stored ${result.sync?.transaction_count || 0} total transaction(s).`;
        await refreshDashboard();
      },
      onExit: (err) => {
        $("plaid-status").textContent = err ? (err.display_message || err.error_message || "Plaid Link exited with an error.") : "Plaid Link closed.";
      },
    });
    handler.open();
  } catch (error) {
    $("plaid-status").textContent = error.code ? `${error.code}: ${error.message}` : error.message;
  }
}

async function syncPlaid() {
  if (!currentUser) return;
  $("plaid-status").textContent = "Syncing Plaid transactions...";
  try {
    const result = await api(`/api/users/${currentUser}/plaid/sync`, { method: "POST" });
    const stored = (result.results || []).reduce((sum, item) => sum + Number(item.stored || 0), 0);
    $("plaid-status").textContent = `Plaid sync complete. ${stored} added or updated, ${result.transaction_count} total transaction(s).`;
    await refreshDashboard();
  } catch (error) {
    $("plaid-status").textContent = error.code ? `${error.code}: ${error.message}` : error.message;
  }
}

function updateCategoryOptions(categories) {
  currentCategories = categories.slice();
  const selected = $("category").value;
  $("category").innerHTML = `<option value="">All categories</option>` + categories.map((cat) => `<option value="${escapeHtml(cat)}">${escapeHtml(cat)}</option>`).join("");
  $("category").value = selected;
  const renameSelected = $("rename-from").value;
  $("rename-from").innerHTML = categories.map((cat) => `<option value="${escapeHtml(cat)}">${escapeHtml(cat)}</option>`).join("");
  $("rename-from").value = categories.includes(renameSelected) ? renameSelected : (categories[0] || "");
  $("manual-category-options").innerHTML = categories.map((cat) => `<option value="${escapeHtml(cat)}"></option>`).join("");
}

function updateUserDetails(payload) {
  $("user-detail-name").textContent = payload.user.name;
  $("user-detail-transactions").textContent = payload.summary.transaction_count;
  $("user-detail-statement-summary").textContent = `${payload.statement_count} PDF statement(s), ${payload.summary.transaction_count} matching transaction(s)`;
  $("user-detail-banks").textContent = payload.bank_names && payload.bank_names.length ? payload.bank_names.join(", ") : "No banks detected yet";
  $("folder-path").textContent = payload.folder;
  $("cache-status").textContent = cacheStatusText(currentScan);
}

function updateAccountOptions(accountTypes) {
  const selected = $("account_type").value;
  const labels = { checking: "Checking", savings: "Savings", credit_card: "Credit card", cash: "Cash", unknown: "Unknown" };
  $("account_type").innerHTML = `<option value="">All accounts</option>` + accountTypes.map((type) => `<option value="${escapeHtml(type)}">${escapeHtml(labels[type] || type)}</option>`).join("");
  $("account_type").value = selected;
}

function loadGraphSettings() {
  try {
    const raw = localStorage.getItem(graphSettingsKey);
    if (!raw) {
      graphSettings = defaultGraphSettings();
      return;
    }
    const saved = JSON.parse(raw);
    chartDefinitions.forEach((chart) => {
      if (saved[chart.key]) {
        const allowedTypes = Array.from($(chart.type).options).map((option) => option.value);
        const allowedPeriods = Array.from($(chart.period).options).map((option) => option.value);
        graphSettings[chart.key] = {
          visible: Boolean(saved[chart.key].visible),
          type: allowedTypes.includes(saved[chart.key].type) ? saved[chart.key].type : chart.defaultType,
          period: allowedPeriods.includes(saved[chart.key].period) ? saved[chart.key].period : chart.defaultPeriod,
        };
      }
    });
  } catch {
    graphSettings = defaultGraphSettings();
  }
}

function saveGraphSettings() {
  localStorage.setItem(graphSettingsKey, JSON.stringify(graphSettings));
}

function syncGraphSettingsForm() {
  chartDefinitions.forEach((chart) => {
    $(chart.visible).checked = graphSettings[chart.key].visible;
    $(chart.type).value = graphSettings[chart.key].type;
    $(chart.period).value = graphSettings[chart.key].period;
  });
  updateMockLayout();
}

function updateMockLayout() {
  chartDefinitions.forEach((chart) => {
    const mock = document.querySelector(`[data-mock-chart="${chart.key}"]`);
    if (mock) mock.classList.toggle("active", $(chart.visible).checked);
  });
}

function applyGraphVisibility() {
  const selected = chartDefinitions.filter((chart) => graphSettings[chart.key].visible);
  const grid = $("visual-grid");
  grid.classList.remove("graph-count-0", "graph-count-1", "graph-count-2", "graph-count-3", "graph-count-4", "graph-count-5", "graph-count-6");
  grid.classList.add(`graph-count-${selected.length}`);
  chartDefinitions.forEach((chart) => {
    $(chart.panel).classList.toggle("hidden", !graphSettings[chart.key].visible);
  });
}

function renderCharts() {
  if (!currentSummary) return;
  const transactions = currentSummary.transactions || [];
  drawTrend("savings", transactions);
  drawTrend("income", transactions);
  drawTrend("expenses", transactions);
  drawCategories(transactions);
  drawAccounts(transactions);
  drawMerchants(transactions);
  applyGraphVisibility();
}

function openGraphModal() {
  syncGraphSettingsForm();
  $("graph-modal").classList.remove("hidden");
  $("graph-modal").setAttribute("aria-hidden", "false");
}

function closeGraphModal() {
  $("graph-modal").classList.add("hidden");
  $("graph-modal").setAttribute("aria-hidden", "true");
}

function openManualModal() {
  const today = new Date().toISOString().slice(0, 10);
  if (!$("manual-date").value) $("manual-date").value = today;
  if (!$("adjustment-date").value) $("adjustment-date").value = today;
  if (!$("manual-account").value) $("manual-account").value = "Cash";
  if (!$("adjustment-account").value) $("adjustment-account").value = "Current holdings";
  $("manual-entry-status").textContent = "Use negative amounts for spending and positive amounts for income.";
  $("adjustment-status").textContent = "This creates an adjustment transaction so Savings matches the amount you enter.";
  $("manual-modal").classList.remove("hidden");
  $("manual-modal").setAttribute("aria-hidden", "false");
  $("manual-description").focus();
}

function closeManualModal() {
  $("manual-modal").classList.add("hidden");
  $("manual-modal").setAttribute("aria-hidden", "true");
}

function selectManualTab(tab) {
  const isAdjustment = tab === "adjustment";
  const isRemove = tab === "remove";
  $("manual-entry-form").classList.toggle("hidden", isAdjustment);
  $("manual-entry-form").classList.toggle("hidden", isAdjustment || isRemove);
  $("savings-adjustment-form").classList.toggle("hidden", !isAdjustment);
  $("manual-remove-section").classList.toggle("hidden", !isRemove);
  $("manual-transaction-tab").classList.toggle("active", !isAdjustment && !isRemove);
  $("manual-adjustment-tab").classList.toggle("active", isAdjustment);
  $("manual-remove-tab").classList.toggle("active", isRemove);
}

function openUserModal() {
  if (currentSummary) updateUserDetails(currentSummary);
  $("user-modal").classList.remove("hidden");
  $("user-modal").setAttribute("aria-hidden", "false");
}

function closeUserModal() {
  $("user-modal").classList.add("hidden");
  $("user-modal").setAttribute("aria-hidden", "true");
}

function chartType(id, fallback) {
  const chart = chartDefinitions.find((item) => item.type === id);
  return chart ? graphSettings[chart.key].type : fallback;
}

function chartPeriod(key) {
  return graphSettings[key]?.period || chartDefinitions.find((item) => item.key === key)?.defaultPeriod || "all";
}

function percent(value, total) {
  return total ? `${(value / total * 100).toFixed(1)}%` : "0.0%";
}

function pieStops(entries, total) {
  let start = 0;
  return entries.map(([, amount], index) => {
    const end = start + (amount / total * 100);
    const segment = `${pieColors[index % pieColors.length]} ${start.toFixed(2)}% ${end.toFixed(2)}%`;
    start = end;
    return segment;
  }).join(", ");
}

function drawPie(chart, entries, emptyMessage) {
  const total = entries.reduce((sum, [, amount]) => sum + amount, 0);
  if (!entries.length || total <= 0) {
    chart.innerHTML = `<div class="chart-empty">${escapeHtml(emptyMessage)}</div>`;
    return;
  }
  chart.innerHTML = `<div class="pie-chart">
    <div class="pie-ring" style="--pie-stops: conic-gradient(${pieStops(entries, total)})"></div>
    <div class="pie-list">
      ${entries.map(([label, amount], index) => `<div class="pie-item">
        <i class="dot pie-${index % pieColors.length}"></i>
        <strong>${escapeHtml(label)}</strong>
        <span>${percent(amount, total)} ${money(amount)}</span>
      </div>`).join("")}
    </div>
  </div>`;
}

const internalCategories = new Set(["Transfers", "Credit Card Payment"]);

function periodKey(dateValue, period) {
  const date = new Date(`${dateValue}T00:00:00`);
  if (Number.isNaN(date.getTime())) return "Unknown";
  const year = date.getFullYear();
  if (period === "year") return String(year);
  if (period === "quarter") return `${year} Q${Math.floor(date.getMonth() / 3) + 1}`;
  if (period === "week") {
    const first = new Date(year, 0, 1);
    const days = Math.floor((date - first) / 86400000);
    const week = Math.floor((days + first.getDay()) / 7) + 1;
    return `${year} W${String(week).padStart(2, "0")}`;
  }
  if (period === "day") return dateValue;
  return dateValue.slice(0, 7) || "Unknown";
}

function startOfPeriodDate(dateValue, period) {
  const date = new Date(`${dateValue}T00:00:00`);
  if (Number.isNaN(date.getTime())) return null;
  if (period === "year") return new Date(date.getFullYear(), 0, 1);
  if (period === "quarter") return new Date(date.getFullYear(), Math.floor(date.getMonth() / 3) * 3, 1);
  if (period === "week") {
    const day = date.getDay();
    const start = new Date(date);
    start.setDate(date.getDate() - day);
    return new Date(start.getFullYear(), start.getMonth(), start.getDate());
  }
  if (period === "day") return new Date(date.getFullYear(), date.getMonth(), date.getDate());
  return new Date(date.getFullYear(), date.getMonth(), 1);
}

function nextPeriodDate(date, period) {
  const next = new Date(date);
  if (period === "year") next.setFullYear(next.getFullYear() + 1);
  else if (period === "quarter") next.setMonth(next.getMonth() + 3);
  else if (period === "week") next.setDate(next.getDate() + 7);
  else if (period === "day") next.setDate(next.getDate() + 1);
  else next.setMonth(next.getMonth() + 1);
  return next;
}

function periodSequence(transactions, period) {
  const dates = transactions
    .map((tx) => startOfPeriodDate(tx.date || "", period))
    .filter(Boolean)
    .sort((a, b) => a - b);
  if (!dates.length) return [];
  const keys = [];
  let cursor = dates[0];
  const last = dates[dates.length - 1];
  while (cursor <= last) {
    keys.push(periodKey(cursor.toISOString().slice(0, 10), period));
    cursor = nextPeriodDate(cursor, period);
  }
  return keys;
}

function periodStart(period) {
  if (period === "all") return "";
  const now = new Date();
  if (period === "month") return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-01`;
  if (period === "quarter") {
    const month = Math.floor(now.getMonth() / 3) * 3 + 1;
    return `${now.getFullYear()}-${String(month).padStart(2, "0")}-01`;
  }
  if (period === "year") return `${now.getFullYear()}-01-01`;
  return "";
}

function transactionsForWindow(transactions, period) {
  const start = periodStart(period);
  return start ? transactions.filter((tx) => (tx.date || "") >= start) : transactions;
}

function groupedTrend(transactions, metric, period) {
  const rows = {};
  const relevant = transactions
    .slice()
    .filter((tx) => !internalCategories.has(tx.category))
    .sort((a, b) => String(a.date || "").localeCompare(String(b.date || "")));
  relevant.forEach((tx) => {
      const key = periodKey(tx.date || "", period);
      const amount = Number(tx.amount || 0);
      rows[key] = rows[key] || { income: 0, expenses: 0, savings: 0 };
      if (amount >= 0) rows[key].income += amount;
      if (amount < 0) rows[key].expenses += Math.abs(amount);
  });
  let running = 0;
  return periodSequence(relevant, period).map((key) => {
    const row = rows[key] || { income: 0, expenses: 0, savings: 0 };
    running += row.income - row.expenses;
    return [key, metric === "savings" ? running : row[metric]];
  });
}

function categoryExpenseEntries(transactions, period = "all", limit = 10) {
  const rows = {};
  transactionsForWindow(transactions, period).forEach((tx) => {
    if (tx.amount < 0 && !internalCategories.has(tx.category)) {
      const category = tx.category || "Uncategorized";
      rows[category] = (rows[category] || 0) + Math.abs(tx.amount);
    }
  });
  return Object.entries(rows)
    .sort((a, b) => b[1] - a[1])
    .slice(0, limit);
}

function drawTrend(metric, transactions) {
  const chart = $(`${metric}-chart`);
  const period = chartPeriod(metric);
  const entries = groupedTrend(transactions, metric, period);
  const labels = { savings: "Savings", income: "Income", expenses: "Expenses" };
  const colorClass = metric === "expenses" ? "expense" : metric === "savings" ? "category" : "income";
  if (!entries.length) {
    chart.innerHTML = `<div class="chart-empty">No ${labels[metric].toLowerCase()} data yet.</div>`;
    return;
  }
  if (chartType(`${metric}-chart-type`, "line") === "line") {
    drawTrendLine(chart, entries, labels[metric], colorClass);
    return;
  }
  const max = Math.max(...entries.map(([, value]) => Math.abs(value)), 1);
  chart.innerHTML = `<div class="chart-legend"><span class="legend-item"><i class="dot ${colorClass}"></i>${escapeHtml(labels[metric])}</span></div>` + entries.map(([label, value]) => {
    const width = Math.max(3, Math.abs(value) / max * 100);
    return `<div class="chart-row">
      <div class="chart-meta"><strong>${escapeHtml(label)}</strong><span>${money(value)}</span></div>
      <div class="track" title="${escapeHtml(`${labels[metric]} - ${label}: ${money(value)}`)}"><div class="bar-${colorClass === "expense" ? "expense" : colorClass === "income" ? "income" : "category"}" style="width:${width}%"></div></div>
    </div>`;
  }).join("");
}

function drawTrendLine(chart, entries, label, colorClass) {
  const width = Math.max(760, entries.length * 86);
  const height = 330;
  const padX = 54;
  const padY = 42;
  const labelPad = 54;
  const values = entries.map(([, value]) => value);
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const span = Math.max(max - min, 1);
  const xFor = (index) => entries.length === 1 ? width / 2 : padX + index * ((width - padX * 2) / (entries.length - 1));
  const yFor = (value) => height - labelPad - ((value - min) / span * (height - padY - labelPad));
  const path = entries.map(([, value], index) => `${index ? "L" : "M"} ${xFor(index).toFixed(1)} ${yFor(value || 0).toFixed(1)}`).join(" ");
  const axisLabels = entries.map(([period], index) => `<text class="line-label" x="${xFor(index).toFixed(1)}" y="${height - 10}" text-anchor="end" transform="rotate(-35 ${xFor(index).toFixed(1)} ${height - 10})">${escapeHtml(period)}</text>`).join("");
  const strokeClass = colorClass === "expense" ? "line-expense" : colorClass === "income" ? "line-income" : "line-category";
  const fill = colorClass === "expense" ? "#f2577a" : colorClass === "income" ? "#00a7a7" : "#7657ff";
  const dots = entries.map(([period, value], index) => `<circle class="line-dot" cx="${xFor(index).toFixed(1)}" cy="${yFor(value || 0).toFixed(1)}" r="5" fill="${fill}">
          <title>${escapeHtml(`${label} - ${period}: ${money(value)}`)}</title>
        </circle>`).join("");
  chart.innerHTML = `<div class="chart-legend"><span class="legend-item"><i class="dot ${colorClass}"></i>${escapeHtml(label)}</span></div>
    <div class="line-chart">
      <svg class="line-svg" style="min-width:${width}px" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(label)} trend line chart">
        <line class="line-grid" x1="${padX}" y1="${height - labelPad}" x2="${width - padX}" y2="${height - labelPad}"></line>
        <line class="line-grid" x1="${padX}" y1="${padY}" x2="${width - padX}" y2="${padY}"></line>
        <path class="${strokeClass}" d="${path}"></path>
        ${dots}
        ${axisLabels}
      </svg>
    </div>`;
}

function drawCategories(transactions) {
  const chart = $("category-chart");
  const entries = categoryExpenseEntries(transactions, chartPeriod("category"));
  if (!entries.length) {
    chart.innerHTML = `<div class="chart-empty">No expense categories yet.</div>`;
    return;
  }
  if (chartType("category-chart-type", "pie") === "pie") {
    drawPie(chart, entries, "No expense categories yet.");
    return;
  }
  const max = Math.max(...entries.map(([, amount]) => amount), 1);
  const total = entries.reduce((sum, [, amount]) => sum + amount, 0);
  chart.innerHTML = `<div class="chart-legend"><span class="legend-item"><i class="dot category"></i>Expense category</span></div>` + entries.map(([cat, amount]) => {
    const width = Math.max(3, amount / max * 100);
    return `<div class="chart-row">
      <div class="chart-meta"><strong>${escapeHtml(cat)}</strong><span>${percent(amount, total)} ${money(amount)}</span></div>
      <div class="track" title="${escapeHtml(`${cat}: ${money(amount)}`)}"><div class="bar-category" style="width:${width}%"></div></div>
    </div>`;
  }).join("");
}

function drawAccounts(transactions) {
  const chart = $("account-chart");
  const rows = {};
  transactionsForWindow(transactions, chartPeriod("account")).forEach((tx) => {
    if (internalCategories.has(tx.category)) return;
    const account = tx.account_name || tx.account_type || "Unknown";
    const row = rows[account] || { income: 0, expenses: 0, net: 0 };
    if (tx.amount >= 0) row.income += tx.amount;
    if (tx.amount < 0) row.expenses += Math.abs(tx.amount);
    row.net = row.income - row.expenses;
    rows[account] = row;
  });
  const entries = Object.entries(rows || {})
    .map(([account, row]) => [account, row.expenses || 0, row.net || 0])
    .sort((a, b) => b[1] - a[1]);
  if (!entries.length) {
    chart.innerHTML = `<div class="chart-empty">No account activity yet.</div>`;
    return;
  }
  if (chartType("account-chart-type", "bar") === "pie") {
    drawPie(chart, entries.map(([account, expenses]) => [account, expenses]).filter(([, expenses]) => expenses > 0).slice(0, 8), "No account spending yet.");
    return;
  }
  const max = Math.max(...entries.map(([, expenses]) => expenses), 1);
  chart.innerHTML = `<div class="chart-legend"><span class="legend-item"><i class="dot expense"></i>Spending</span></div>` + entries.map(([account, expenses, net]) => {
    const width = Math.max(3, expenses / max * 100);
    return `<div class="chart-row">
      <div class="chart-meta"><strong>${escapeHtml(account)}</strong><span>${money(expenses)} spent - ${money(net)} net</span></div>
      <div class="track" title="${escapeHtml(`${account}: ${money(expenses)} spent, ${money(net)} net`)}"><div class="bar-expense" style="width:${width}%"></div></div>
    </div>`;
  }).join("");
}

function drawMerchants(transactions) {
  const chart = $("merchant-chart");
  const rows = {};
  transactionsForWindow(transactions, chartPeriod("merchant")).forEach((tx) => {
    if (tx.amount < 0 && !internalCategories.has(tx.category)) {
      const merchant = merchantPatternFromDescription(tx.description) || "Unknown merchant";
      rows[merchant] = (rows[merchant] || 0) + Math.abs(tx.amount);
    }
  });
  const entries = Object.entries(rows || {}).sort((a, b) => b[1] - a[1]).slice(0, 12);
  if (!entries.length) {
    chart.innerHTML = `<div class="chart-empty">No merchant spending yet.</div>`;
    return;
  }
  if (chartType("merchant-chart-type", "bar") === "pie") {
    drawPie(chart, entries.slice(0, 8), "No merchant spending yet.");
    return;
  }
  const max = Math.max(...entries.map(([, amount]) => amount), 1);
  chart.innerHTML = `<div class="chart-legend"><span class="legend-item"><i class="dot category"></i>Merchant spend</span></div>` + entries.map(([merchant, amount]) => {
    const width = Math.max(3, amount / max * 100);
    return `<div class="chart-row">
      <div class="chart-meta"><strong>${escapeHtml(merchant)}</strong><span>${money(amount)}</span></div>
      <div class="track" title="${escapeHtml(`${merchant}: ${money(amount)}`)}"><div class="bar-category" style="width:${width}%"></div></div>
    </div>`;
  }).join("");
}

function categoryOptions(selected) {
  const categories = currentCategories.includes(selected) ? currentCategories : currentCategories.concat([selected]);
  return categories.map((cat) => `<option value="${escapeHtml(cat)}" ${cat === selected ? "selected" : ""}>${escapeHtml(cat)}</option>`).join("");
}

function merchantPatternFromDescription(description) {
  return String(description || "")
    .replace(/\b(web id|ppd id|transaction#|inv_|ticket number|agreement number)[:\w\- ]*/ig, "")
    .replace(/\b\d{3}[- ]?\d{3}[- ]?\d{4}\b/g, "")
    .replace(/\b\d{6,}\b/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 64);
}

function drawTransactions(transactions) {
  if (!transactions.length) {
    $("transactions").innerHTML = `<tr><td colspan="7">No transactions found for the current filters.</td></tr>`;
    return;
  }
  $("transactions").innerHTML = transactions.slice().reverse().map((tx) => {
    const cls = tx.amount < 0 ? "negative" : "positive";
    const pattern = merchantPatternFromDescription(tx.description);
    return `<tr>
      <td data-label="Date">${escapeHtml(tx.date)}</td>
      <td data-label="Account">${escapeHtml(tx.account_name || tx.account_type || "Unknown")}</td>
      <td data-label="Description">${escapeHtml(tx.description)}</td>
      <td data-label="Category"><select class="category-picker" data-transaction-id="${escapeHtml(tx.id)}">${categoryOptions(tx.category)}</select></td>
      <td data-label="Amount" class="amount ${cls}">${money(tx.amount)}</td>
      <td data-label="Statement">${escapeHtml(tx.statement)}</td>
      <td data-label="Rule"><button type="button" class="secondary rule-button" data-merchant-pattern="${escapeHtml(pattern)}" data-category="${escapeHtml(tx.category)}">Use Merchant</button></td>
    </tr>`;
  }).join("");
}

function drawManualTransactionList(transactions) {
  const target = $("manual-transaction-list");
  if (!target) return;
  if (!transactions.length) {
    target.innerHTML = `<div class="manual-empty">No manual transactions have been added yet.</div>`;
    return;
  }
  target.innerHTML = transactions.slice().reverse().map((tx) => {
    const cls = tx.amount < 0 ? "negative" : "positive";
    return `<div class="manual-transaction-item">
      <div>
        <strong>${escapeHtml(tx.description)}</strong>
        <span>${escapeHtml(tx.date)} - ${escapeHtml(tx.account_name || "Manual")} - ${escapeHtml(tx.category || "Uncategorized")}</span>
      </div>
      <b class="${cls}">${money(tx.amount)}</b>
      <button type="button" class="secondary remove-manual-button" data-transaction-id="${escapeHtml(tx.id)}">Remove</button>
    </div>`;
  }).join("");
}

async function refreshAdminUsers() {
  if (!currentIsAdmin) return;
  const payload = await api(`/api/admin/${currentUser}/users`);
  const users = payload.users || [];
  const nonAdminUsers = users.filter((user) => !user.is_admin);
  $("admin-total-users").textContent = nonAdminUsers.length;
  $("admin-total-statements").textContent = nonAdminUsers.reduce((sum, user) => sum + Number(user.statement_count || 0), 0);
  $("admin-total-transactions").textContent = nonAdminUsers.reduce((sum, user) => sum + Number(user.transaction_count || 0), 0);
  const target = $("admin-user-list");
  if (!nonAdminUsers.length) {
    target.innerHTML = `<div class="manual-empty">No users found.</div>`;
    return;
  }
  target.innerHTML = nonAdminUsers.map((user) => `<div class="admin-user-item">
    <div>
      <strong>${escapeHtml(user.name)}</strong>
      <span>${escapeHtml(user.id)} - ${user.statement_count || 0} statement(s) - ${user.transaction_count || 0} transaction(s)</span>
    </div>
    <input class="admin-password-input" type="password" placeholder="New password" autocomplete="new-password" data-user-id="${escapeHtml(user.id)}">
    <button type="button" class="secondary admin-password-button" data-user-id="${escapeHtml(user.id)}">Change</button>
    <button type="button" class="secondary admin-remove-button" data-user-id="${escapeHtml(user.id)}">Remove</button>
  </div>`).join("");
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[char]));
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await loginWithUsername(event.currentTarget);
  } catch (error) {
    $("login-status").textContent = error.code ? `${error.code}: ${error.message}` : error.message;
  }
});

$("admin-create-user-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!currentIsAdmin) return;
  const formElement = event.currentTarget;
  const form = new FormData(formElement);
  $("admin-status").textContent = "Adding user...";
  try {
    await api(`/api/admin/${currentUser}/users`, { method: "POST", body: form });
    $("admin-status").textContent = "User added.";
    formElement.reset();
    await refreshAdminUsers();
  } catch (error) {
    $("admin-status").textContent = error.code ? `${error.code}: ${error.message}` : error.message;
  }
});

$("admin-user-list").addEventListener("click", async (event) => {
  if (!currentIsAdmin) return;
  const passwordButton = event.target.closest(".admin-password-button");
  const removeButton = event.target.closest(".admin-remove-button");
  if (!passwordButton && !removeButton) return;
  const userId = (passwordButton || removeButton).dataset.userId;
  try {
    if (passwordButton) {
      const input = passwordButton.closest(".admin-user-item")?.querySelector(".admin-password-input");
      const password = input ? input.value : "";
      if (!password) {
        $("admin-status").textContent = "Enter a new password for that user.";
        return;
      }
      const form = new FormData();
      form.set("password", password);
      $("admin-status").textContent = "Changing password...";
      await api(`/api/admin/${currentUser}/users/${encodeURIComponent(userId)}/password`, { method: "POST", body: form });
      $("admin-status").textContent = `Password changed for ${userId}.`;
      input.value = "";
    }
    if (removeButton) {
      $("admin-status").textContent = "Removing user...";
      await api(`/api/admin/${currentUser}/users/${encodeURIComponent(userId)}/delete`, { method: "POST" });
      $("admin-status").textContent = `Removed ${userId}.`;
      await refreshAdminUsers();
    }
  } catch (error) {
    $("admin-status").textContent = error.code ? `${error.code}: ${error.message}` : error.message;
  }
});

$("transactions").addEventListener("change", async (event) => {
  const picker = event.target.closest(".category-picker");
  if (!picker || !currentUser) return;
  const form = new FormData();
  form.set("transaction_id", picker.dataset.transactionId);
  form.set("category", picker.value);
  $("category-status").textContent = "Saving category override...";
  await api(`/api/users/${currentUser}/transactions/category`, { method: "POST", body: form });
  $("category-status").textContent = "Category override saved.";
  await refreshDashboard();
});

$("rename-category-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!currentUser) return;
  const form = new FormData(event.currentTarget);
  if (!form.get("old_category") || !form.get("new_category")) return;
  $("category-status").textContent = "Renaming category...";
  const result = await api(`/api/users/${currentUser}/categories/rename`, { method: "POST", body: form });
  $("category-status").textContent = `Renamed ${result.updated_count} transaction(s).`;
  $("rename-to").value = "";
  await refreshDashboard();
});

$("merchant-rule-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!currentUser) return;
  const form = new FormData(event.currentTarget);
  if (!form.get("pattern") || !form.get("category")) return;
  $("category-status").textContent = "Applying merchant rule...";
  const result = await api(`/api/users/${currentUser}/merchant-rules`, { method: "POST", body: form });
  $("category-status").textContent = `Merchant rule applied to ${result.updated_count} transaction(s).`;
  await refreshDashboard();
});

$("manual-entry-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!currentUser) return;
  const formElement = event.currentTarget;
  const form = new FormData(formElement);
  $("manual-entry-status").textContent = "Saving manual transaction...";
  try {
    const result = await api(`/api/users/${currentUser}/transactions/manual`, { method: "POST", body: form });
    $("manual-entry-status").textContent = `Saved to ${result.document}`;
    formElement.reset();
    closeManualModal();
    $("category-status").textContent = "Manual transaction saved.";
    await refreshDashboard();
  } catch (error) {
    $("manual-entry-status").textContent = error.message;
  }
});

$("savings-adjustment-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!currentUser) return;
  const formElement = event.currentTarget;
  const form = new FormData(formElement);
  $("adjustment-status").textContent = "Saving balance adjustment...";
  try {
    const result = await api(`/api/users/${currentUser}/transactions/adjustment`, { method: "POST", body: form });
    $("adjustment-status").textContent = `Saved to ${result.document}`;
    formElement.reset();
    closeManualModal();
    $("category-status").textContent = "Savings adjustment saved.";
    await refreshDashboard();
  } catch (error) {
    $("adjustment-status").textContent = error.message;
  }
});

$("manual-transaction-list").addEventListener("click", async (event) => {
  const button = event.target.closest(".remove-manual-button");
  if (!button || !currentUser) return;
  const form = new FormData();
  form.set("transaction_id", button.dataset.transactionId);
  button.disabled = true;
  $("manual-remove-status").textContent = "Removing manual transaction...";
  try {
    const result = await api(`/api/users/${currentUser}/transactions/manual/delete`, { method: "POST", body: form });
    $("manual-remove-status").textContent = `Removed from ${result.document}`;
    $("category-status").textContent = "Manual transaction removed.";
    await refreshDashboard();
  } catch (error) {
    $("manual-remove-status").textContent = error.message;
    button.disabled = false;
  }
});

$("transactions").addEventListener("click", (event) => {
  const button = event.target.closest(".rule-button");
  if (!button) return;
  $("merchant-pattern").value = button.dataset.merchantPattern || "";
  $("merchant-category").value = button.dataset.category || "";
  $("merchant-pattern").focus();
  $("category-status").textContent = "Merchant rule form filled from that transaction. Edit the text if needed, then apply.";
});

async function refreshStatementFolder() {
  if (!currentUser) return;
  const button = $("refresh-statements");
  button.disabled = true;
  setBusy("Refreshing statement folder and checking for new PDFs...");
  $("category-status").textContent = "Scanning mapped folder for new PDFs...";
  try {
    const result = await api(`/api/users/${currentUser}/scan`, { method: "POST" });
    currentScan = result;
    await refreshDashboard();
    updateCacheStatus(currentScan);
    const errorText = result.errors && result.errors.length ? ` ${result.errors.length} statement(s) had parsing errors.` : "";
    $("category-status").textContent = `Refresh complete: ${result.statement_count} PDF statement(s), ${result.transaction_count} transaction(s). ${cacheStatusText(result)}.${errorText}`;
  } catch (error) {
    $("category-status").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

$("refresh-statements").addEventListener("click", refreshStatementFolder);
$("connect-plaid").addEventListener("click", connectPlaid);
$("sync-plaid").addEventListener("click", syncPlaid);

$("open-manual-entry").addEventListener("click", openManualModal);
$("close-manual-entry").addEventListener("click", closeManualModal);
$("manual-modal").addEventListener("click", (event) => {
  if (event.target === $("manual-modal")) closeManualModal();
});
document.querySelectorAll("[data-manual-tab]").forEach((button) => {
  button.addEventListener("click", () => selectManualTab(button.dataset.manualTab));
});

$("open-user-modal").addEventListener("click", openUserModal);
$("admin-refresh-users").addEventListener("click", async () => {
  if (currentIsAdmin) await refreshAdminUsers();
});
$("close-user-modal").addEventListener("click", closeUserModal);
$("user-modal").addEventListener("click", (event) => {
  if (event.target === $("user-modal")) closeUserModal();
});

$("open-graph-settings").addEventListener("click", openGraphModal);
$("close-graph-settings").addEventListener("click", closeGraphModal);
$("graph-modal").addEventListener("click", (event) => {
  if (event.target === $("graph-modal")) closeGraphModal();
});
$("graph-settings-form").addEventListener("input", updateMockLayout);
$("graph-settings-form").addEventListener("submit", (event) => {
  event.preventDefault();
  chartDefinitions.forEach((chart) => {
    graphSettings[chart.key] = {
      visible: $(chart.visible).checked,
      type: $(chart.type).value || chart.defaultType,
      period: $(chart.period).value || chart.defaultPeriod,
    };
  });
  saveGraphSettings();
  syncGraphSettingsForm();
  renderCharts();
  closeGraphModal();
});

["query", "category", "account_type", "start", "end"].forEach((id) => $(id).addEventListener("input", () => refreshDashboard()));
$("clear-filters").addEventListener("click", () => {
  ["query", "category", "account_type", "start", "end"].forEach((id) => $(id).value = "");
  refreshDashboard();
});


function setCurrentMonthFilter() {
  const now = new Date();
  $("start").value = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-01`;
  $("end").value = "";
  refreshDashboard();
}

function setLargeExpenseFilter() {
  $("query").value = "";
  $("category").value = "";
  $("account_type").value = "";
  refreshDashboard().then(() => {
    if (!currentSummary) return;
    const expenses = (currentSummary.transactions || []).filter((tx) => tx.amount < 0 && Math.abs(tx.amount) >= 100);
    drawTransactions(expenses);
    $("activity-detail").textContent = "Showing expenses of $100 or more.";
  });
}

function setSubscriptionFilter() {
  const words = ["subscription", "netflix", "spotify", "apple", "google", "prime", "membership", "hulu", "disney", "adobe", "icloud"];
  $("query").value = "";
  refreshDashboard().then(() => {
    if (!currentSummary) return;
    const matches = (currentSummary.transactions || []).filter((tx) => {
      const text = `${tx.description || ""} ${tx.category || ""}`.toLowerCase();
      return words.some((word) => text.includes(word));
    });
    drawTransactions(matches);
    $("activity-detail").textContent = "Showing likely subscription or membership transactions.";
  });
}

function resetCommandView() {
  ["query", "category", "account_type", "start", "end"].forEach((id) => $(id).value = "");
  refreshDashboard();
}

$("quick-this-month").addEventListener("click", setCurrentMonthFilter);
$("quick-large-expenses").addEventListener("click", setLargeExpenseFilter);
$("quick-subscriptions").addEventListener("click", setSubscriptionFilter);
$("quick-reset-view").addEventListener("click", resetCommandView);

const remembered = localStorage.getItem("financialReviewUser");
if (remembered) {
  $("username").value = remembered;
}
initThemeToggle();
loadGraphSettings();
syncGraphSettingsForm();
applyGraphVisibility();
resumeSavedSession();
