const $ = (selector) => document.querySelector(selector);
const token = location.hash.slice(1) || sessionStorage.getItem("work-hub-token");
if (location.hash && token) {
  sessionStorage.setItem("work-hub-token", token);
  history.replaceState(null, "", location.pathname);
}

const content = $("#content");
const states = ["inbox", "todo", "doing", "waiting", "done", "skipped", "canceled"];
const labels = { todo: "To do", doing: "In progress" };
const caption = (value) => labels[value] || value.charAt(0).toUpperCase() + value.slice(1);
const element = (tag, text, className) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
};
let view = "inbox";
let special = "";
let layout = "list";
let requestNumber = 0;

async function api(path, data) {
  const options = { headers: { Authorization: "Bearer " + (token || "") } };
  if (data !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(data);
  }
  const response = await fetch("/api/" + path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error);
  return result;
}

function showError(error) {
  $("#message").textContent = error.message;
}

function card(item) {
  const row = element("article", undefined, "card");
  const body = element("div");
  const title = element("button", item.title, "item-title");
  title.type = "button";
  title.addEventListener("click", () => showItem(item.id).catch(showError));
  body.append(title);
  const status = item.connection?.verification === "unknown" ? "Source unknown"
    : item.connection?.source_status;
  const meta = [caption(item.kind), item.due && "Due " + item.due,
    item.blocked_by?.length && "Blocked", status,
    item.progress && `${item.progress.done}/${item.progress.total} complete`].filter(Boolean);
  body.append(element("p", meta.join(" · "), "meta"));
  const action = item.next_action || item.documentation?.next_action;
  if (action) body.append(element("p", action, "meta"));
  const select = element("select");
  select.setAttribute("aria-label", "Status for " + item.title);
  for (const state of states) {
    const option = element("option", caption(state));
    option.value = state;
    option.selected = state === item.status;
    select.append(option);
  }
  select.addEventListener("change", async () => {
    try {
      await api("status", { id: item.id, status: select.value });
      await render();
    } catch (error) {
      select.value = item.status;
      showError(error);
    }
  });
  row.append(body, select);
  return row;
}

async function showItem(id) {
  const {item, history: events} = await api("history?" + new URLSearchParams({id}));
  const detail = $("#detail-content");
  detail.replaceChildren(element("h2", item.title));
  if (item.description) detail.append(element("p", item.description));
  const action = item.next_action || item.documentation?.next_action;
  if (action) detail.append(element("p", action));
  if (item.documentation?.stage) detail.append(element("p", "Draft stage · " + item.documentation.stage, "meta"));
  if (item.watch) {
    detail.append(element("p", "Last successful check · " + (item.watch.last_success_at || "Pending"), "meta"));
    if (item.watch.error) detail.append(element("p", item.watch.error, "meta"));
  }
  for (const url of [item.url, ...(item.links || [])].filter(Boolean)) {
    if (!/^https?:\/\//.test(url)) continue;
    const link = element("a", url);
    link.href = url;
    link.target = "_blank";
    link.rel = "noreferrer";
    const paragraph = element("p");
    paragraph.append(link);
    detail.append(paragraph);
  }
  detail.append(element("h3", "History"));
  const list = element("ul");
  for (const event of events) {
    list.append(element("li", [
      event.at.slice(0, 10), caption(event.kind.replaceAll("_", " ")), event.status || event.message
    ].filter(Boolean).join(" · ")));
  }
  detail.append(list);
  $("#details").showModal();
}

function line(title, description) {
  const row = element("article", undefined, "card");
  const text = element("div");
  text.append(element("strong", title), element("p", description, "meta"));
  row.append(text);
  return row;
}

async function renderSpecial(current) {
  const data = await api(current);
  const list = element("div", undefined, "list");
  if (current === "agenda") {
    for (const event of data.calendar_events) list.append(line(event.title, event.start + " · " + event.calendar));
    for (const item of data.items) list.append(card(item));
    for (const [name, errors] of Object.entries(data.calendar_errors)) list.append(line(name, errors.join("\n")));
  } else if (current === "review") {
    list.append(line(data.since + " – " + data.through, `${data.completed.length} completed · ${data.actual_minutes} minutes tracked`));
    for (const item of data.time_by_item) {
      list.append(line(item.title, `${item.actual_minutes} min actual · ${item.planned_minutes ?? "—"} min planned`));
    }
    for (const habit of data.habits) list.append(line(habit.title, `${habit.done}/${habit.due} check-ins`));
    for (const item of data.waiting) list.append(line(item.title, "Waiting"));
    for (const item of data.carried_over) list.append(line(item.title, "Carried over"));
  } else {
    for (const notice of data) {
      const row = line(notice.message, notice.at.slice(0, 10));
      const dismiss = element("button", "Dismiss", "close");
      dismiss.addEventListener("click", () => api("notifications", {sequence: notice.sequence}).then(render).catch(showError));
      row.append(dismiss);
      list.append(row);
    }
  }
  return list.childElementCount ? list : element("p", "Nothing scheduled here yet.", "empty");
}

async function render() {
  const sequence = ++requestNumber;
  const current = special;
  const q = $("#search").value.trim();
  $("#message").textContent = "";
  $("#heading").textContent = current === "review" ? "Weekly review" : caption(current || (q ? "search" : view));
  $("#toolbar").hidden = Boolean(current);
  for (const button of document.querySelectorAll("nav button")) {
    button.setAttribute("aria-current", button.dataset.special === current ||
      (!current && button.dataset.view === view) ? "page" : "false");
  }
  if (current) {
    const section = await renderSpecial(current);
    if (sequence === requestNumber) content.replaceChildren(section);
    return;
  }
  const params = new URLSearchParams(q ? {q, view: "all"} : {view});
  const items = await api("items?" + params);
  if (sequence !== requestNumber) return;
  $("#count").textContent = items.length + (items.length === 1 ? " item" : " items");
  $("#layout").textContent = layout === "list" ? "Board view" : "List view";
  if (!items.length) {
    content.replaceChildren(element("p", q ? "No matching items." : "Nothing in this view yet.", "empty"));
    return;
  }
  const group = element("div", undefined, layout);
  if (layout === "board") {
    for (const state of states) {
      const rows = items.filter((item) => item.status === state);
      if (!rows.length) continue;
      const lane = element("section", undefined, "lane list");
      lane.append(element("h2", caption(state) + " · " + rows.length), ...rows.map(card));
      group.append(lane);
    }
  } else group.append(...items.map(card));
  content.replaceChildren(group);
}

$("#capture").addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.currentTarget));
  try {
    await api("items", data);
    $("#capture").reset();
    $("#search").value = "";
    special = "";
    view = data.kind === "task" || data.kind === "habit" ? "inbox" : "all";
    await render();
  } catch (error) { showError(error); }
});

$("#search").addEventListener("input", () => {
  special = "";
  render().catch(showError);
});
$("#layout").addEventListener("click", () => {
  layout = layout === "list" ? "board" : "list";
  render().catch(showError);
});
for (const button of document.querySelectorAll("[data-special]")) {
  button.addEventListener("click", () => {
    special = button.dataset.special;
    $("#search").value = "";
    render().catch(showError);
  });
}
$("#date").textContent = new Intl.DateTimeFormat(undefined, {dateStyle: "full"}).format(new Date());

api("config").then((config) => {
  $("#workspace").textContent = caption(config.workspace);
  for (const [name, filters] of Object.entries(config.views)) {
    const button = element("button", caption(name));
    button.type = "button";
    button.dataset.view = name;
    button.addEventListener("click", () => {
      view = name;
      layout = filters.layout || "list";
      special = "";
      $("#search").value = "";
      render().catch(showError);
    });
    $("#navigation").append(button);
  }
  return render();
}).catch(showError);
