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
let config;

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
  row.dataset.color = item.color && item.color !== "default" ? item.color : (item.status === "done" ? "green"
    : item.status === "waiting" || item.blocked_by?.length ? "amber"
    : ({project: "purple", milestone: "blue", habit: "teal", watch: "blue"})[item.kind] || "default");
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
  const today = new Intl.DateTimeFormat("sv-SE").format(new Date());
  const overdue = item.due && item.due < today && !["done", "skipped", "canceled"].includes(item.status);
  const labels = [
    [item.missed ? "Missed" : overdue ? "Overdue" : "", "urgent"],
    [item.status === "waiting" || item.blocked_by?.length ? "Waiting" : "", "waiting"],
    [item.status === "done" ? "Done" : "", "done"],
    ...(item.tags || []).map(tag => [tag, "tag"])
  ];
  const badges = element("div", undefined, "badges");
  for (const [text, tone] of labels) {
    if (!text) continue;
    const badge = element("span", text, "badge");
    badge.dataset.tone = tone;
    badges.append(badge);
  }
  if (badges.childElementCount) body.append(badges);
  const action = item.next_action || item.documentation?.next_action;
  if (action) body.append(element("p", action, "meta"));
  const select = element("select");
  select.setAttribute("aria-label", "Status for " + item.title);
  for (const state of states) {
    const option = element("option", caption(state));
    option.value = state;
    option.selected = state === item.status;
    option.disabled = item.missed && !["done", "skipped", "canceled"].includes(state);
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

function control(name, label, type = "text", value = "", choices = []) {
  const wrapper = element("label", label);
  const input = element(type === "select" ? "select" : "input");
  input.name = name;
  if (type === "select") {
    for (const [key, text] of choices) {
      const option = element("option", text);
      option.value = key;
      input.append(option);
    }
  } else input.type = type;
  input.value = value ?? "";
  wrapper.append(input);
  return wrapper;
}

function choices(values, empty = "") {
  return [...(empty ? [["", empty]] : []), ...values.map(value => [value, caption(value)])];
}

function actionButton(text, fn) {
  const button = element("button", text, "close");
  button.type = "button";
  button.addEventListener("click", () => Promise.resolve().then(fn).catch(showError));
  return button;
}

function inputDate(value) {
  if (!value) return "";
  const date = new Date(value);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

async function showItem(id) {
  const {item, dependencies, history: events} = await api("history?" + new URLSearchParams({id}));
  const detail = $("#detail-content");
  detail.replaceChildren(element("h2", item.title));
  const action = item.next_action || item.documentation?.next_action;
  if (item.documentation?.stage) detail.append(element("p", "Draft stage · " + item.documentation.stage, "meta"));
  if (action) detail.append(element("p", action, "meta"));
  const form = element("form", undefined, "settings-form");
  const error = element("p", "", "error");
  error.setAttribute("role", "status");
  const grid = element("div", undefined, "settings-grid");
  grid.append(
    control("due", "Due date", "date", item.due),
    control("color", "Color", "select", item.color || "default", choices(config.colors)),
    control("scheduled", "Schedule", "datetime-local", inputDate(item.scheduled)),
    control("remind", "Reminder", "datetime-local", inputDate(item.remind_at)),
    control("snooze", "Snooze until", "datetime-local", inputDate(item.snoozed_until)),
    control("tags", "Tags (comma separated)", "text", item.tags.join(", "))
  );
  form.append(control("title", "Title", "text", item.title), control("next_action", "Next action", "text", item.next_action),
    control("description", "Description", "text", item.description), grid, element("button", "Save changes", "primary"), error);
  form.elements.title.required = true;
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(form));
    data.tags = data.tags.split(",").map(tag => tag.trim()).filter(Boolean);
    for (const field of ["scheduled", "remind", "snooze"]) {
      if (data[field] === inputDate(item[{remind: "remind_at", snooze: "snoozed_until"}[field] || field])) delete data[field];
    }
    try {
      await api("edit", {id, ...data});
      await render();
      await showItem(id);
    } catch (failure) { error.textContent = failure.message; }
  });
  detail.append(form);
  if (item.watch) {
    detail.append(element("p", "Last successful check · " + (item.watch.last_success_at || "Pending"), "meta"));
    if (item.watch.error) detail.append(element("p", item.watch.error, "meta"));
    for (const action of item.status === "waiting" ? ["resume"] : ["check", "pause"]) {
      detail.append(actionButton(action === "check" ? "Check now" : caption(action), async () => {
        await api("watches", {id, action});
        await showItem(id);
        await render();
      }));
    }
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
  detail.append(element("h3", "Dependencies"));
  const linked = element("ul", undefined, "detail-list");
  for (const dependency of dependencies) {
    const row = element("li");
    row.append(element("span", dependency.title + " · " + caption(dependency.status)),
      actionButton("Remove", async () => {
        await api("dependencies", {id, target: dependency.id, action: "remove"});
        await showItem(id);
        await render();
      }));
    linked.append(row);
  }
  detail.append(linked);
  const candidates = (await api("items?view=all")).filter(candidate => candidate.id !== id
    && !dependencies.some(dependency => dependency.id === candidate.id));
  if (candidates.length) {
    const dependency = element("form", undefined, "settings-form");
    dependency.append(control("target", "Blocked by", "select", "",
      [["", "Select an item"], ...candidates.map(candidate => [candidate.id, candidate.title])]),
      element("button", "Add dependency", "primary"));
    dependency.elements.target.required = true;
    dependency.addEventListener("submit", async event => {
      event.preventDefault();
      try {
        await api("dependencies", {id, target: dependency.elements.target.value, action: "add"});
        await showItem(id);
        await render();
      } catch (failure) { error.textContent = failure.message; }
    });
    detail.append(dependency);
  }
  detail.append(element("h3", "History"));
  const list = element("ul");
  for (const event of events) {
    list.append(element("li", [
      event.at.slice(0, 10), caption(event.kind.replaceAll("_", " ")), event.status || event.message
    ].filter(Boolean).join(" · ")));
  }
  detail.append(list);
  if (!$("#details").open) $("#details").showModal();
}

function watchForm() {
  const form = element("form", undefined, "settings-form watch-form");
  form.append(control("title", "Watch title"), control("target", "URL or Radar path"),
    control("kind", "Source type", "select", "page", choices(config.watch_types)),
    control("contains", "Keyword (optional)"), control("every", "Check every (minutes)", "number", 60),
    element("button", "Add watch", "primary"));
  form.elements.title.required = form.elements.target.required = true;
  form.elements.every.required = true;
  form.elements.every.min = 1;
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(form));
    data.every = Number(data.every);
    try {
      await api("watches", {action: "add", ...data});
      await render();
    } catch (error) { showError(error); }
  });
  return form;
}

function line(title, description) {
  const row = element("article", undefined, "card");
  const text = element("div");
  text.append(element("strong", title), element("p", description, "meta"));
  row.append(text);
  return row;
}

async function renderSpecial(current) {
  const list = element("div", undefined, "list");
  if (current === "watches") {
    const [watches, service] = await Promise.all([api("items?view=all&kind=watch"), api("service")]);
    const last = service.last_success_at ? new Date(service.last_success_at).toLocaleString() : "Pending";
    list.append(line("Background checks", [
      service.loaded ? "Login schedule active" : "Login schedule inactive",
      "Last successful run · " + last,
      (service.missed_total || 0) + " recorded missed intervals",
      ...(service.currently_missed ? [service.currently_missed + " intervals since the last start"] : []),
      ...(service.errors || [])
    ].join("\n")), watchForm(), ...watches.map(card));
    return list;
  }
  const data = await api(current);
  if (current === "agenda") {
    for (const event of data.calendar_events) list.append(line(event.title, event.start + " · " + event.calendar));
    for (const item of data.items) list.append(card(item));
    for (const [name, errors] of Object.entries(data.calendar_errors)) list.append(line(name, errors.join("\n")));
  } else if (current === "review") {
    list.append(line(data.since + " – " + data.through, `${data.completed.length} completed · ${data.actual_minutes} minutes tracked`));
    for (const item of data.time_by_item) {
      list.append(line(item.title, `${item.actual_minutes} min actual · ${item.planned_minutes ?? "—"} min planned`));
    }
    if (data.missed_habit_check_ins) list.append(line("Missed check-ins", String(data.missed_habit_check_ins)));
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
      lane.dataset.lane = state;
      lane.append(element("h2", caption(state) + " · " + rows.length), ...rows.map(card));
      group.append(lane);
    }
  } else group.append(...items.map(card));
  content.replaceChildren(group);
}

$("#capture").addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.currentTarget));
  if (!data.repeat || !["task", "habit"].includes(data.kind)) delete data.repeat;
  try {
    await api("items", data);
    $("#capture").reset();
    $("#repeat-control").hidden = true;
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

const viewFields = {
  status: ["Status", states.concat("all")],
  kind: ["Item type", []],
  source: ["Source", []],
  due: ["Due date", ["today", "overdue", "upcoming"]],
  color: ["Color", []],
  layout: ["Layout", ["list", "board"]]
};
const builtinViews = ["inbox", "today", "waiting", "upcoming", "projects", "all"];

async function loadConfig() {
  config = await api("config");
  $("#workspace").textContent = caption(config.workspace);
  $("#navigation").replaceChildren();
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
}

$("#kind").addEventListener("change", event => {
  $("#repeat-control").hidden = !["task", "habit"].includes(event.target.value);
  if (event.target.value === "habit") $("#repeat-control select").value = "daily";
});
$("#save-view").addEventListener("click", () => {
  const form = $("#view-form");
  const existing = builtinViews.includes(view) ? {} : config.views[view];
  form.elements.name.value = existing === config.views[view] ? view : "";
  form.elements.q.value = $("#search").value || existing?.q || "";
  $("#view-error").textContent = "";
  $("#remove-view").hidden = !form.elements.name.value;
  $("#view-filters").replaceChildren(
    ...Object.entries(viewFields).map(([key, [label, options]]) => {
      const available = {kind: config.kinds, source: config.sources, color: config.colors}[key] || options;
      return control(key, label, "select", existing?.[key] || (key === "layout" ? layout : ""),
        choices(available, "Any"));
    }),
    control("tag", "Tag", "text", existing?.tag)
  );
  $("#view-dialog").showModal();
});
$("#view-form").addEventListener("submit", async event => {
  event.preventDefault();
  const {name, ...filters} = Object.fromEntries(new FormData(event.currentTarget));
  try {
    const active = config.views[name.trim()] || {};
    const allowed = Object.fromEntries(Object.entries(filters).filter(([, value]) => value));
    for (const key of ["parent", "waiting", "snoozed"]) {
      if (key in active) allowed[key] = active[key];
    }
    await api("views", {name: name.trim(), filters: allowed});
    await loadConfig();
    view = name.trim();
    layout = allowed.layout || "list";
    $("#search").value = "";
    $("#view-dialog").close();
    await render();
  } catch (error) { $("#view-error").textContent = error.message; }
});
$("#remove-view").addEventListener("click", async () => {
  try {
    await api("views", {action: "remove", name: view});
    await loadConfig();
    view = "inbox";
    $("#view-dialog").close();
    await render();
  } catch (error) { $("#view-error").textContent = error.message; }
});

loadConfig().then(render).catch(showError);
