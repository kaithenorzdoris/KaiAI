const formatNumber = (value) => new Intl.NumberFormat("vi-VN").format(value ?? 0);
let dashboardSession = { guide_ai_enabled: false, csrf_token: "", chat_visitor_id: "" };
let lastChatMessageId = 0;
let chatPollTimer = null;
let chatUnreadCount = 0;
let chatHasLoaded = false;
let serverPage = 1;
let serverQuery = "";
let serverTotalPages = 1;
let serverSearchTimer = null;
let serverRequestVersion = 0;
let latestStatus = null;
let uptimeTimer = null;

async function getJson(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response.json();
}

function showMessage(container, text) {
  container.replaceChildren();
  const message = document.createElement("div");
  message.className = "empty-state";
  message.textContent = text;
  container.append(message);
}

let allCommands = [];
let selectedCategory = "all";

function renderCommands() {
  const container = document.querySelector("#command-list");
  const query = document.querySelector("#command-search").value.trim().toLocaleLowerCase("vi");
  const visible = allCommands.filter((command) => {
    const matchesCategory = selectedCategory === "all" || command.category === selectedCategory;
    const matchesQuery = `${command.name} ${command.description} ${command.usage}`
      .toLocaleLowerCase("vi").includes(query);
    return matchesCategory && matchesQuery;
  });
  container.replaceChildren();
  if (!visible.length) {
    showMessage(container, allCommands.length ? "Không tìm thấy lệnh phù hợp." : "Danh sách lệnh sẽ hiện khi bot kết nối.");
    return;
  }
  for (const command of visible) {
    const row = document.createElement("article");
    row.className = "command-row";
    const badge = document.createElement("span");
    badge.className = `command-badge${command.type === "Slash" ? " slash" : ""}`;
    badge.textContent = command.type === "Slash" ? "/" : "!";
    const info = document.createElement("div");
    info.className = "command-info";
    const name = document.createElement("strong");
    name.textContent = command.usage || command.name;
    const description = document.createElement("span");
    description.textContent = command.description;
    info.append(name, description);
    const category = document.createElement("span");
    category.className = "command-category";
    category.textContent = command.category;
    row.append(badge, info, category);
    container.append(row);
  }
}

function renderCategories() {
  const container = document.querySelector("#command-categories");
  container.replaceChildren();
  const categories = ["all", ...new Set(allCommands.map((command) => command.category))];
  for (const category of categories) {
    const button = document.createElement("button");
    button.className = `tag${category === selectedCategory ? " active" : ""}`;
    button.dataset.category = category;
    button.textContent = category === "all" ? "Tất cả" : category;
    button.addEventListener("click", () => {
      selectedCategory = category;
      renderCategories();
      renderCommands();
    });
    container.append(button);
  }
}

function renderServers(data) {
  const container = document.querySelector("#server-list");
  serverTotalPages = data.total_pages || 1;
  serverPage = data.page || 1;
  document.querySelector("#server-count").textContent = formatNumber(data.network_total ?? data.total);
  document.querySelector("#server-result-count").textContent =
    serverQuery ? `${formatNumber(data.total)} kết quả` : `${formatNumber(data.network_total ?? data.total)} server`;
  document.querySelector("#server-page-label").textContent =
    data.total ? `Trang ${formatNumber(serverPage)} / ${formatNumber(serverTotalPages)}` : "Không có server";
  document.querySelector("#server-prev").disabled = serverPage <= 1;
  document.querySelector("#server-next").disabled = serverPage >= serverTotalPages;
  container.replaceChildren();
  if (!data.servers.length) {
    showMessage(container, serverQuery ? "Không tìm thấy server phù hợp." : "Bot hiện chưa tham gia server nào.");
    return;
  }
  for (const server of data.servers) {
    const row = document.createElement("div");
    row.className = "server-row";
    const avatar = document.createElement("span");
    avatar.className = "server-avatar";
    if (server.icon) {
      const image = document.createElement("img");
      image.src = server.icon;
      image.alt = "";
      image.loading = "lazy";
      avatar.append(image);
    } else {
      avatar.textContent = server.name.slice(0, 1).toUpperCase();
    }
    const name = document.createElement("strong");
    name.className = "server-name";
    name.textContent = server.name;
    const members = document.createElement("span");
    members.className = "server-meta";
    members.textContent = `${formatNumber(server.members)} thành viên`;
    row.append(avatar, name, members);
    container.append(row);
  }
}

async function loadServers(page = 1) {
  serverPage = page;
  const requestVersion = ++serverRequestVersion;
  const params = new URLSearchParams({ page: String(page), per_page: "8" });
  if (serverQuery) params.set("q", serverQuery);
  try {
    const data = await getJson(`/api/servers?${params}`);
    if (requestVersion !== serverRequestVersion) return;
    renderServers(data);
  } catch {
    if (requestVersion !== serverRequestVersion) return;
    showMessage(document.querySelector("#server-list"), "Không thể tải danh sách server.");
  }
}

function formatUptime(seconds) {
  const total = Math.max(0, Math.floor(seconds || 0));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const parts = [];
  if (days) parts.push(`${days} ngày`);
  if (hours || days) parts.push(`${hours} giờ`);
  parts.push(`${minutes} phút`);
  return parts.join(" ");
}

function renderLiveStatus(status) {
  latestStatus = status;
  const online = Boolean(status.ready);
  const state = online ? "Trực tuyến" : "Đang khởi động";
  const statusLine = document.querySelector("#hero-status-line");
  const presenceDot = document.querySelector(".hero-copy .pulse-dot");
  statusLine.textContent = online
    ? `KAI ĐANG THỨC CÙNG ${formatNumber(status.servers)} SERVER`
    : "KAI ĐANG CHỜ KẾT NỐI";
  presenceDot.classList.toggle("is-offline", !online);
  document.querySelector("#hero-network-copy").textContent =
    status.servers === 1
      ? "Cùng 1 server Discord"
      : `Cùng ${formatNumber(status.servers)} server Discord`;
  document.querySelector("#bot-status").textContent = online ? "đang hoạt động" : "đang khởi động";
  document.querySelector("#stat-status").textContent = state;
  document.querySelector(".footer-status i").style.background = online ? "#67e0c0" : "#e8bd65";
  document.querySelector("#stat-servers").textContent = formatNumber(status.servers);
  document.querySelector("#stat-members").textContent = formatNumber(status.members);
  document.querySelector("#stat-latency").textContent =
    Number.isFinite(status.latency_ms) ? `${formatNumber(status.latency_ms)} ms` : "Chưa có";
  document.querySelector("#stat-uptime").textContent = `Uptime ${formatUptime(status.uptime_seconds)}`;
  if (status.invite_url) {
    document.querySelectorAll(".invite-link").forEach((link) => { link.href = status.invite_url; });
  }
  loadServers(serverPage);
  document.querySelector("#server-count").dataset.networkCount = String(status.servers);
}

function renderLeaderboard(data) {
  const container = document.querySelector("#leaderboard-list");
  container.replaceChildren();
  if (!data.available) {
    showMessage(container, "Bảng xếp hạng kinh tế chưa được khởi tạo.");
    return;
  }
  if (!data.players.length) {
    showMessage(container, "Chưa có dữ liệu người chơi.");
    return;
  }
  for (const player of data.players.slice(0, 7)) {
    const row = document.createElement("div");
    row.className = "leader-row";
    const rank = document.createElement("span");
    rank.className = `rank${player.rank === 1 ? " gold" : player.rank === 2 ? " silver" : player.rank === 3 ? " bronze" : ""}`;
    rank.textContent = `#${player.rank}`;
    const name = document.createElement("strong");
    name.className = "leader-name";
    name.textContent = player.name;
    const wealth = document.createElement("span");
    wealth.className = "leader-wealth";
    wealth.textContent = `${formatNumber(player.wealth)} ₫`;
    row.append(rank, name, wealth);
    container.append(row);
  }
}

function renderGameLeaderboard(data) {
  const container = document.querySelector("#game-leaderboard-list");
  container.replaceChildren();
  if (!data.available) {
    showMessage(container, "Thành tích game sẽ hiện khi dữ liệu trò chơi sẵn sàng.");
    return;
  }
  if (!data.players.length) {
    showMessage(container, "Chưa có thành tích game. Hãy bắt đầu một ván trong server!");
    return;
  }
  for (const player of data.players) {
    const row = document.createElement("div");
    row.className = "leader-row";
    const rank = document.createElement("span");
    rank.className = `rank${player.rank === 1 ? " gold" : player.rank === 2 ? " silver" : player.rank === 3 ? " bronze" : ""}`;
    rank.textContent = `#${player.rank}`;
    const name = document.createElement("strong");
    name.className = "leader-name";
    name.textContent = player.name;
    const stats = document.createElement("span");
    stats.className = "leader-wealth";
    stats.textContent = `${formatNumber(player.wins)} thắng · ${formatNumber(player.played)} ván · ${player.profit >= 0 ? "+" : ""}${formatNumber(player.profit)} ₫`;
    row.append(rank, name, stats);
    container.append(row);
  }
}

async function initializeDashboard() {
  const results = await Promise.allSettled([
    getJson("/api/status"),
    getJson("/api/commands"),
    getJson("/api/servers"),
    getJson("/api/leaderboard"),
    getJson("/api/games/leaderboard"),
    getJson("/api/session"),
  ]);
  const [status, commands, servers, leaderboard, gamesLeaderboard, userSession] = results;

  if (status.status === "fulfilled") {
    renderLiveStatus(status.value);
  } else {
    document.querySelector("#bot-status").textContent = "không thể kết nối";
    document.querySelector("#stat-status").textContent = "Ngoại tuyến";
    document.querySelector("#hero-status-line").textContent = "KAI ĐANG CHỜ TÍN HIỆU";
    document.querySelector("#hero-network-copy").textContent = "Chưa đọc được trạng thái";
    document.querySelector(".hero-copy .pulse-dot").classList.add("is-offline");
  }
  if (commands.status === "fulfilled") {
    allCommands = commands.value.commands;
    document.querySelector("#command-count").textContent = formatNumber(allCommands.length);
    renderCategories();
    renderCommands();
  } else {
    showMessage(document.querySelector("#command-list"), "Không thể tải danh sách lệnh. Hãy thử tải lại trang.");
  }
  if (servers.status === "fulfilled") renderServers({
    ...servers.value,
    network_total: status.status === "fulfilled" ? status.value.servers : servers.value.total,
  });
  else showMessage(document.querySelector("#server-list"), "Không thể tải thông tin server.");
  if (leaderboard.status === "fulfilled") renderLeaderboard(leaderboard.value);
  else showMessage(document.querySelector("#leaderboard-list"), "Không thể tải bảng xếp hạng.");
  if (gamesLeaderboard.status === "fulfilled") renderGameLeaderboard(gamesLeaderboard.value);
  else showMessage(document.querySelector("#game-leaderboard-list"), "Không thể tải thành tích game.");

  if (userSession.status === "fulfilled") {
    dashboardSession = userSession.value;
    document.querySelector("#assistant-mode").textContent =
      userSession.value.guide_ai_enabled ? "AI đã bật · không lưu hội thoại" : "Chế độ hướng dẫn nhanh";
    document.querySelector(".assistant-disclaimer").textContent = userSession.value.guide_ai_enabled
      ? "Câu hỏi hiện tại được gửi tới OpenAI để trả lời; không gửi lịch sử chat."
      : "Hướng dẫn nhanh dùng nội dung công khai của website.";
    configureCommunityChat();
    configureServerDirectory();
    configureSiteAssistant();
    if (!uptimeTimer) {
      uptimeTimer = window.setInterval(() => {
        if (latestStatus) {
          latestStatus.uptime_seconds += 1;
          document.querySelector("#stat-uptime").textContent =
            `Uptime ${formatUptime(latestStatus.uptime_seconds)}`;
        }
      }, 1000);
      window.setInterval(async () => {
        try {
          renderLiveStatus(await getJson("/api/status"));
        } catch {
          document.querySelector("#stat-status").textContent = "Không thể kết nối";
          document.querySelector("#bot-status").textContent = "không thể kết nối";
        }
      }, 30000);
      window.setInterval(async () => {
        try {
          renderLeaderboard(await getJson("/api/leaderboard"));
        } catch {
          showMessage(document.querySelector("#leaderboard-list"), "Không thể cập nhật bảng xếp hạng.");
        }
      }, 60000);
    }
  }
}

document.querySelector("#command-search").addEventListener("input", renderCommands);
document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    document.querySelector("#command-search").focus();
    document.querySelector("#commands").scrollIntoView({ behavior: "smooth" });
  }
});
document.querySelector(".mobile-menu").addEventListener("click", (event) => {
  const open = document.querySelector(".topbar").classList.toggle("menu-open");
  event.currentTarget.setAttribute("aria-expanded", String(open));
});
document.querySelectorAll(".main-nav a").forEach((link) => {
  link.addEventListener("click", () => {
    document.querySelector(".topbar").classList.remove("menu-open");
    document.querySelector(".mobile-menu").setAttribute("aria-expanded", "false");
  });
});
if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
  document.addEventListener("pointermove", (event) => {
    const art = document.querySelector(".hero-art");
    if (!art || event.pointerType === "touch") return;
    const x = (event.clientX / window.innerWidth - 0.5) * 7;
    const y = (event.clientY / window.innerHeight - 0.5) * -7;
    art.style.setProperty("--pointer-x", `${x}deg`);
    art.style.setProperty("--pointer-y", `${y}deg`);
  }, { passive: true });
}
initializeDashboard();

function setChatNotice(message, isError = false) {
  const note = document.querySelector("#chat-signin-note");
  note.textContent = message;
  note.classList.toggle("chat-error", isError);
}

function renderChatMessage(message) {
  const container = document.querySelector("#chat-messages");
  const emptyMessage = container.querySelector(".chat-empty");
  if (emptyMessage) emptyMessage.remove();
  const row = document.createElement("article");
  row.className = "chat-message";
  const avatar = document.createElement("span");
  avatar.className = "chat-avatar";
  if (message.avatar) {
    const image = document.createElement("img");
    image.src = message.avatar;
    image.alt = "";
    image.loading = "lazy";
    avatar.append(image);
  } else {
    avatar.textContent = message.username.slice(0, 1).toUpperCase();
  }
  const body = document.createElement("div");
  body.className = "chat-message-body";
  const metadata = document.createElement("div");
  metadata.className = "chat-message-meta";
  const username = document.createElement("strong");
  username.textContent = message.username;
  const guest = document.createElement("span");
  guest.className = "chat-guest";
  guest.textContent = "Khách";
  guest.title = "Biệt danh do người gửi tự chọn";
  const time = document.createElement("time");
  time.dateTime = new Date(message.created_at * 1000).toISOString();
  time.textContent = new Intl.DateTimeFormat("vi-VN", {
    hour: "2-digit",
    minute: "2-digit",
  }).format(message.created_at * 1000);
  const content = document.createElement("p");
  content.textContent = message.content;
  metadata.append(username, guest, time);
  body.append(metadata, content);
  row.append(avatar, body);
  container.append(row);
  while (container.children.length > 150) container.firstElementChild.remove();
}

async function loadChatMessages() {
  const after = lastChatMessageId ? `?after=${lastChatMessageId}` : "";
  const container = document.querySelector("#chat-messages");
  try {
    const response = await fetch(`/api/chat${after}`, { headers: { Accept: "application/json" } });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "chat_unavailable");
    for (const message of payload.messages) {
      renderChatMessage(message);
      lastChatMessageId = Math.max(lastChatMessageId, message.id);
      if (chatHasLoaded && message.user_id !== dashboardSession.chat_visitor_id
          && document.querySelector("#chat-panel").hidden) {
        chatUnreadCount += 1;
      }
    }
    if (payload.messages.length) {
      container.scrollTop = container.scrollHeight;
      updateChatUnread();
    } else if (!lastChatMessageId && !container.children.length) {
      const welcome = document.createElement("div");
      welcome.className = "chat-empty";
      welcome.textContent = "Chưa có tin nhắn. Hãy là người đầu tiên chào mọi người!";
      container.append(welcome);
    }
    chatHasLoaded = true;
    setChatNotice("Bạn đang trò chuyện với cộng đồng KaiAI bằng biệt danh khách.");
  } catch (error) {
    setChatNotice(
      error.message === "chat_unavailable"
        ? "Chat đang tạm thời không khả dụng. Hãy thử lại sau."
        : "Không thể kết nối chat. Kiểm tra kết nối mạng rồi thử lại.",
      true,
    );
  }
}

function updateChatUnread() {
  const badge = document.querySelector("#chat-unread");
  badge.hidden = chatUnreadCount === 0;
  badge.textContent = chatUnreadCount > 99 ? "99+" : String(chatUnreadCount);
}

function configureCommunityChat() {
  const panel = document.querySelector("#chat-panel");
  const launcher = document.querySelector("#chat-launcher");
  const input = document.querySelector("#chat-input");
  const nameInput = document.querySelector("#chat-name");
  const send = document.querySelector("#chat-send");

  const setOpen = (open) => {
    panel.hidden = !open;
    launcher.setAttribute("aria-expanded", String(open));
    launcher.setAttribute("aria-label", open ? "Đóng chat cộng đồng" : "Mở chat cộng đồng");
    launcher.classList.toggle("is-open", open);
    if (open) document.dispatchEvent(new Event("close-site-assistant"));
    if (open) {
      chatUnreadCount = 0;
      updateChatUnread();
      loadChatMessages();
      input.focus();
    }
  };
  document.addEventListener("close-community-chat", () => {
    if (!panel.hidden) setOpen(false);
  });
  if (!chatPollTimer) {
    loadChatMessages();
    chatPollTimer = window.setInterval(loadChatMessages, 4000);
  }
  launcher.addEventListener("click", () => setOpen(panel.hidden));
  document.querySelector("#chat-minimize").addEventListener("click", () => setOpen(false));
  document.querySelector("#chat-close").addEventListener("click", () => setOpen(false));

  document.querySelector("#chat-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const content = input.value.trim();
    const username = nameInput.value.trim();
    if (!content || send.disabled) return;
    send.disabled = true;
    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": dashboardSession.csrf_token,
        },
        body: JSON.stringify({ content, username }),
      });
      const result = await response.json();
      if (response.status === 429) {
        setChatNotice("Bạn đang gửi hơi nhanh. Chờ một chút rồi thử lại nhé.", true);
      } else if (!response.ok) {
        setChatNotice(
          result.error === "username_length"
            ? "Biệt danh cần từ 2 đến 24 ký tự."
            : result.error === "message_length"
              ? "Tin nhắn phải có từ 1 đến 500 ký tự."
            : "Tin nhắn chưa gửi được. Hãy thử lại.",
          true,
        );
      } else {
        input.value = "";
        input.style.height = "auto";
        setChatNotice("Tin nhắn đã được gửi.");
        await loadChatMessages();
      }
    } catch {
      setChatNotice("Không thể gửi tin do lỗi kết nối. Hãy thử lại.", true);
    } finally {
      send.disabled = false;
      input.focus();
    }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 112)}px`;
  });

  setupChatDragging();
  setupChatResizing();
}

const chatLayoutKey = "kaiai-community-chat-layout";

function readChatLayout() {
  try {
    const stored = JSON.parse(localStorage.getItem(chatLayoutKey) || "{}");
    return stored && typeof stored === "object" ? stored : {};
  } catch {
    return {};
  }
}

function saveChatLayout({ savePanel = true, saveLauncher = true } = {}) {
  const layout = readChatLayout();
  const panel = document.querySelector("#chat-panel");
  const launcher = document.querySelector("#chat-launcher");
  if (savePanel && !panel.hidden) {
    const panelRect = panel.getBoundingClientRect();
    layout.panel = {
      left: panelRect.left,
      top: panelRect.top,
      width: panelRect.width,
      height: panelRect.height,
    };
  }
  if (saveLauncher) {
    const launcherRect = launcher.getBoundingClientRect();
    layout.launcher = { left: launcherRect.left, top: launcherRect.top };
  }
  try {
    localStorage.setItem(chatLayoutKey, JSON.stringify(layout));
  } catch {
    // Private browsing may disable local storage; the chat remains usable.
  }
}

function applyChatLayout() {
  const saved = readChatLayout();
  const panel = document.querySelector("#chat-panel");
  const launcher = document.querySelector("#chat-launcher");
  const clamp = (value, min, max) => Math.min(Math.max(value, min), max);
  if (saved.panel && Number.isFinite(saved.panel.left) && Number.isFinite(saved.panel.top)) {
    const width = clamp(saved.panel.width || 390, 300, Math.min(window.innerWidth - 24, 560));
    const height = clamp(saved.panel.height || 540, 360, Math.min(window.innerHeight - 96, 760));
    panel.style.width = `${width}px`;
    panel.style.height = `${height}px`;
    panel.style.left = `${clamp(saved.panel.left, 12, window.innerWidth - width - 12)}px`;
    panel.style.top = `${clamp(saved.panel.top, 76, window.innerHeight - height - 12)}px`;
    panel.style.right = "auto";
    panel.style.bottom = "auto";
  }
  if (saved.launcher && Number.isFinite(saved.launcher.left) && Number.isFinite(saved.launcher.top)) {
    const rect = launcher.getBoundingClientRect();
    launcher.style.left = `${clamp(saved.launcher.left, 8, window.innerWidth - rect.width - 8)}px`;
    launcher.style.top = `${clamp(saved.launcher.top, 8, window.innerHeight - rect.height - 8)}px`;
    launcher.style.right = "auto";
    launcher.style.bottom = "auto";
  }
}

function setupChatDragging() {
  const panel = document.querySelector("#chat-panel");
  const handle = document.querySelector("#chat-drag-handle");
  const launcher = document.querySelector("#chat-launcher");
  applyChatLayout();

  handle.addEventListener("pointerdown", (event) => {
    if (event.target.closest("button")) return;
    const rect = panel.getBoundingClientRect();
    const startX = event.clientX;
    const startY = event.clientY;
    const offsetX = startX - rect.left;
    const offsetY = startY - rect.top;
    handle.setPointerCapture(event.pointerId);
    const move = (pointerEvent) => {
      const width = panel.offsetWidth;
      const height = panel.offsetHeight;
      panel.style.left = `${Math.max(8, Math.min(window.innerWidth - width - 8, pointerEvent.clientX - offsetX))}px`;
      panel.style.top = `${Math.max(70, Math.min(window.innerHeight - height - 8, pointerEvent.clientY - offsetY))}px`;
      panel.style.right = "auto";
      panel.style.bottom = "auto";
    };
    const finish = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", finish);
      handle.removeEventListener("pointercancel", finish);
      saveChatLayout({ saveLauncher: false });
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", finish);
    handle.addEventListener("pointercancel", finish);
  });

  let moved = false;
  launcher.addEventListener("pointerdown", (event) => {
    const rect = launcher.getBoundingClientRect();
    const offsetX = event.clientX - rect.left;
    const offsetY = event.clientY - rect.top;
    const startX = event.clientX;
    const startY = event.clientY;
    moved = false;
    launcher.setPointerCapture(event.pointerId);
    const move = (pointerEvent) => {
      if (Math.abs(pointerEvent.clientX - startX) + Math.abs(pointerEvent.clientY - startY) > 6) moved = true;
      if (!moved) return;
      launcher.style.left = `${Math.max(8, Math.min(window.innerWidth - rect.width - 8, pointerEvent.clientX - offsetX))}px`;
      launcher.style.top = `${Math.max(8, Math.min(window.innerHeight - rect.height - 8, pointerEvent.clientY - offsetY))}px`;
      launcher.style.right = "auto";
      launcher.style.bottom = "auto";
      pointerEvent.preventDefault();
    };
    const finish = () => {
      launcher.removeEventListener("pointermove", move);
      launcher.removeEventListener("pointerup", finish);
      launcher.removeEventListener("pointercancel", finish);
      if (moved) saveChatLayout({ savePanel: false });
    };
    launcher.addEventListener("pointermove", move);
    launcher.addEventListener("pointerup", finish);
    launcher.addEventListener("pointercancel", finish);
  });
  launcher.addEventListener("click", (event) => {
    if (!moved) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    moved = false;
  }, true);
  window.addEventListener("resize", applyChatLayout);
}

function setupChatResizing() {
  const panel = document.querySelector("#chat-panel");
  const handle = document.querySelector("#chat-resize-handle");
  handle.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    const rect = panel.getBoundingClientRect();
    const startX = event.clientX;
    const startY = event.clientY;
    const move = (pointerEvent) => {
      const width = Math.max(300, Math.min(window.innerWidth - rect.left - 8, rect.width + pointerEvent.clientX - startX));
      const height = Math.max(360, Math.min(window.innerHeight - rect.top - 8, rect.height + pointerEvent.clientY - startY));
      panel.style.width = `${width}px`;
      panel.style.height = `${height}px`;
    };
    handle.setPointerCapture(event.pointerId);
    const finish = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", finish);
      handle.removeEventListener("pointercancel", finish);
      saveChatLayout({ saveLauncher: false });
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", finish);
    handle.addEventListener("pointercancel", finish);
  });
}

function configureServerDirectory() {
  const search = document.querySelector("#server-search");
  search.addEventListener("input", () => {
    if (serverSearchTimer) window.clearTimeout(serverSearchTimer);
    serverSearchTimer = window.setTimeout(() => {
      serverQuery = search.value.trim();
      loadServers(1);
    }, 220);
  });
  document.querySelector("#server-prev").addEventListener("click", () => {
    if (serverPage > 1) loadServers(serverPage - 1);
  });
  document.querySelector("#server-next").addEventListener("click", () => {
    if (serverPage < serverTotalPages) loadServers(serverPage + 1);
  });
}

function normalizeAssistantQuestion(value) {
  return value.toLocaleLowerCase("vi")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/đ/g, "d")
    .trim();
}

function answerAssistantQuestion(question) {
  const normalized = normalizeAssistantQuestion(question);
  if (/\b(invite|moi bot|them bot|add bot|cach moi|link moi)\b/.test(normalized)) {
    return {
      text: "Link mời chính thức chỉ xin các quyền cơ bản để đọc và gửi tin nhắn, dùng thread, embed, file và reaction — không xin Administrator. Một số lệnh quản trị hoặc voice cần bạn cấp quyền riêng tương ứng trong Discord.",
      action: { label: "Mời KaiAI", href: latestStatus?.invite_url || "#invite", external: true },
    };
  }
  if (/\b(server|guild|may server|bao nhieu server|danh sach server|ten server)\b/.test(normalized)) {
    document.querySelector("#community").scrollIntoView({ behavior: "smooth" });
    return {
      text: `KaiAI hiện đang ở ${formatNumber(latestStatus?.servers || 0)} server, với khoảng ${formatNumber(latestStatus?.members || 0)} thành viên cộng gộp. Danh sách tên server có ô tìm kiếm và phân trang ngay trong mục Cộng đồng.`,
      action: { label: "Xem danh sách server", href: "#community" },
    };
  }
  if (/\b(game|games|tro choi|thanh tich|topgame)\b/.test(normalized)) {
    document.querySelector("#community").scrollIntoView({ behavior: "smooth" });
    return {
      text: "KaiAI có bảng thành tích game trực tiếp trên website và trong Discord. Bảng cộng gộp số ván, số lần thắng và lợi nhuận tiền ảo từ các trò chơi; dùng `!topgame` hoặc `!gameboard` trong server.",
      action: { label: "Xem bảng game", href: "#community" },
    };
  }
  if (/\b(command|commands|lenh|help|su dung|prefix)\b/.test(normalized)) {
    document.querySelector("#commands").scrollIntoView({ behavior: "smooth" });
    return {
      text: `Trang đang đồng bộ ${formatNumber(allCommands.length)} lệnh prefix, slash và bí danh từ bot đang chạy. Dùng ô tìm kiếm hoặc chọn nhóm; trong Discord bạn cũng có thể dùng !help.`,
      action: { label: "Khám phá các lệnh", href: "#commands" },
    };
  }
  if (/\b(chat|cong dong|nhan tin|gui tin|xac minh|verified|email)\b/.test(normalized)) {
    return {
      text: "Phòng chat chung mở cho mọi người, không cần tài khoản. Chọn biệt danh 2–24 ký tự rồi gửi tin tối đa 500 ký tự; hệ thống giới hạn 5 tin trong 15 giây để giảm spam.",
      action: { label: "Mở chat cộng đồng", href: "#community-chat" },
    };
  }
  if (/\b(login|dang nhap|tai khoan|oauth)\b/.test(normalized)) {
    return {
      text: "KaiAI không yêu cầu tạo tài khoản hoặc đăng nhập trên website. Bạn có thể khám phá lệnh, xem trạng thái bot và trò chuyện bằng biệt danh khách.",
      action: { label: "Khám phá KaiAI", href: "#features" },
    };
  }
  if (/\b(ai|tinh nang|lam duoc gi|gioi thieu|kai|la gi)\b/.test(normalized)) {
    return {
      text: `KaiAI là bot Discord đa năng do ${"KaithenorzDoris"} tạo ra: AI chat và tạo ảnh (khi cấu hình provider), game cùng kinh tế ảo, tiện ích cộng đồng và công cụ quản trị. Đây là trang giới thiệu, danh mục lệnh, trạng thái live, danh sách server, bảng xếp hạng và phòng chat chung.`,
      action: { label: "Khám phá tính năng", href: "#features" },
    };
  }
  if (/\b(owner|chu bot|tac gia|creator|kaithenorz)\b/.test(normalized)) {
    return {
      text: "KaiAI được tạo và vận hành bởi KaithenorzDoris. Bạn có thể mời bot vào server hoặc dùng chat cộng đồng để kết nối với mọi người.",
      action: { label: "Xem giới thiệu", href: "#guide" },
    };
  }
  if (/\b(status|online|trang thai|uptime|latency|ping)\b/.test(normalized)) {
    return {
      text: `Trạng thái hiện tại: ${latestStatus?.ready ? "đang trực tuyến" : "chưa sẵn sàng"}, ở ${formatNumber(latestStatus?.servers || 0)} server, độ trễ Gateway ${Number.isFinite(latestStatus?.latency_ms) ? `${latestStatus.latency_ms} ms` : "chưa có dữ liệu"}. Thẻ trạng thái tự cập nhật định kỳ.`,
      action: { label: "Xem trạng thái", href: "#top" },
    };
  }
  return {
    text: "Mình có thể giúp bạn về tính năng KaiAI, lệnh bot, cách mời bot, số/tên server, đăng nhập Discord hoặc chat cộng đồng. Bạn thử hỏi cụ thể một chủ đề nhé!",
  };
}

function appendAssistantMessage(text, type) {
  const transcript = document.querySelector("#assistant-transcript");
  const message = document.createElement("div");
  message.className = `assistant-message assistant-${type}`;
  message.textContent = text;
  transcript.append(message);
  transcript.scrollTop = transcript.scrollHeight;
}

function appendAssistantAction(action) {
  if (!action) return;
  const transcript = document.querySelector("#assistant-transcript");
  const link = document.createElement("a");
  link.className = "assistant-action";
  link.href = action.href;
  link.textContent = action.label;
  if (action.external) {
    link.target = "_blank";
    link.rel = "noopener noreferrer";
  }
  link.addEventListener("click", (event) => {
    if (action.href === "#community-chat") {
      event.preventDefault();
      document.querySelector("#chat-launcher").click();
    }
  });
  transcript.append(link);
  transcript.scrollTop = transcript.scrollHeight;
}

function configureSiteAssistant() {
  const panel = document.querySelector("#assistant-panel");
  const launcher = document.querySelector("#assistant-launcher");
  const input = document.querySelector("#assistant-input");
  const form = document.querySelector("#assistant-form");
  const submit = form.querySelector("button[type='submit']");
  let assistantBusy = false;
  const close = () => {
    panel.hidden = true;
    launcher.setAttribute("aria-expanded", "false");
  };
  document.addEventListener("close-site-assistant", close);
  launcher.addEventListener("click", () => {
    panel.hidden = !panel.hidden;
    launcher.setAttribute("aria-expanded", String(!panel.hidden));
    if (!panel.hidden) {
      document.dispatchEvent(new Event("close-community-chat"));
      input.focus();
    }
  });
  document.querySelector("#assistant-close").addEventListener("click", close);
  const ask = async (question) => {
    const value = question.trim();
    if (!value || assistantBusy) return;
    assistantBusy = true;
    appendAssistantMessage(value, "user");
    const pending = document.createElement("div");
    pending.className = "assistant-message assistant-bot";
    pending.textContent = "Để Kai nghĩ một chút nhé...";
    document.querySelector("#assistant-transcript").append(pending);
    submit.disabled = true;
    document.querySelectorAll("#assistant-prompts button").forEach((button) => {
      button.disabled = true;
    });
    let response = answerAssistantQuestion(value);
    if (dashboardSession.guide_ai_enabled) {
      try {
        const result = await fetch("/api/guide", {
          method: "POST",
          headers: {
            Accept: "application/json",
            "Content-Type": "application/json",
            "X-CSRF-Token": dashboardSession.csrf_token,
          },
          body: JSON.stringify({ question: value }),
        });
        const payload = await result.json();
        if (!result.ok) {
          const message = payload.error === "rate_limited"
            ? "Kai cần nghỉ một chút trước khi trả lời tiếp. Mình sẽ dùng hướng dẫn nhanh trên trang nhé. "
            : "AI đang tạm thời bận. Mình sẽ dùng hướng dẫn nhanh trên trang nhé. ";
          response = { ...response, text: message + response.text };
        } else if (typeof payload.answer === "string" && payload.answer.trim()) {
          response = { ...response, text: payload.answer.trim() };
        }
      } catch {
        response = {
          ...response,
          text: `Kai chưa kết nối được dịch vụ AI. Mình sẽ dùng thông tin nhanh trên trang nhé. ${response.text}`,
        };
      }
    }
    pending.remove();
    appendAssistantMessage(response.text, "bot");
    appendAssistantAction(response.action);
    assistantBusy = false;
    submit.disabled = false;
    document.querySelectorAll("#assistant-prompts button").forEach((button) => {
      button.disabled = false;
    });
  };
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    ask(input.value);
    input.value = "";
  });
  document.querySelectorAll("#assistant-prompts button").forEach((button) => {
    button.addEventListener("click", () => ask(button.dataset.question || ""));
  });
}
