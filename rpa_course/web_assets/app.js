// Управление вкладками панели, запросами к API и показом результатов обработки.
'use strict';

/** Возвращает элемент панели по его ID. */
const byId = (id) => document.getElementById(id);
const filters = byId('filters');
const statusFilter = filters.elements.namedItem('status');
const statusTabs = document.querySelectorAll('[data-status]');
const period = byId('period');
const periodTrigger = byId('period-trigger');
// Берём подписи из самого списка, чтобы названия в фильтре и таблице совпадали.
const statuses = Object.fromEntries([...statusFilter.options].map(({ value, text }) => [value, text]));
const detail = byId('detail');
const detailBody = byId('detail-body');
const runButton = byId('run-start');
const runStatus = byId('run-status');
const runLog = byId('run-log');
const scheduleForm = byId('schedule');
const timeZone = 'Asia/Irkutsk';
const timeFormat = new Intl.DateTimeFormat('ru-RU', {
  timeZone, dateStyle: 'short', timeStyle: 'short',
});
const eventTimeFormat = new Intl.DateTimeFormat('ru-RU', { timeZone, dateStyle: 'short', timeStyle: 'medium' });
const eventStages = {
  cycle: 'Цикл', startup: 'Запуск', price: 'Загрузка прайса', intake: 'Приём письма',
  analysis: 'Анализ заказа', calculation: 'Расчёт', reply: 'Подготовка ответа',
  archive: 'Архивация', delivery: 'Отправка ответа', notification: 'Уведомление', journal: 'Журнал',
};
const eventStatuses = {
  started: 'Начат', ready: 'Готово', loaded: 'Загружен', completed: 'Завершён', failed: 'Остановлен',
  quote: 'КП', clarification: 'Уточнение', prepared: 'Подготовлен', saved: 'Сохранён',
};
const moneyFormat = new Intl.NumberFormat('ru-RU', { style: 'currency', currency: 'RUB' });
/** Показывает дату и время обращения по Иркутску. */
const dateText = (value) => value ? timeFormat.format(new Date(value)) : '—';
/** Форматирует сумму в рублях и оставляет прочерк для отсутствующей суммы. */
const money = (value) => value == null ? '—' : moneyFormat.format(value);
/** Выбирает тему письма или подпись для обращения без темы. */
const subject = (item) => item.subject || (item.status === 'skipped' ? 'Пропущенное письмо' : 'Без темы');
/** Возвращает номер КП или название вида ответа. */
const responseType = (item) => item.quote_number || { quote: 'КП', clarification: 'Уточнение' }[item.outcome];
let page = 1;
let searchTimer;
let listRequest;
let eventsRequest;
let eventsPage = 1;
let detailRequest;
let lastRun;

/** Создаёт узел DOM и вставляет содержимое как обычный текст. */
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

/** Формирует цветную подпись статуса для таблицы или карточки. */
function statusPill(status, label = statuses[status] || status) {
  return element('span', label, `pill ${status}`);
}

/** Выполняет запрос панели и возвращает JSON или сообщение об ошибке API. */
async function read(url, signal, method = 'GET', payload) {
  const options = { signal, method };
  if (payload !== undefined) {
    options.body = JSON.stringify(payload);
    options.headers = { 'Content-Type': 'application/json' };
    options.keepalive = true;
  }
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || data.note || 'Не удалось загрузить данные.');
  return data;
}

/** Загружает расписание, сразу сохраняет переключатель или применяет интервал по кнопке. */
async function updateSchedule(event) {
  event?.preventDefault();
  const controls = byId('schedule-controls');
  const enabled = byId('schedule-enabled');
  const interval = byId('schedule-interval');
  const status = byId('schedule-status');
  const toggleOnly = event?.type === 'change';
  const payload = event ? { enabled: enabled.checked } : undefined;
  if (event && !toggleOnly) payload.interval_minutes = interval.valueAsNumber;
  controls.disabled = true;
  status.textContent = '';
  status.classList.remove('error-text');
  try {
    const settings = await read('/api/schedule', undefined, event ? 'POST' : 'GET', payload);
    if (settings.available === false) return;
    scheduleForm.hidden = false;
    enabled.checked = enabled.defaultChecked = settings.enabled;
    if (!toggleOnly) interval.value = settings.interval_minutes;
    if (toggleOnly) status.textContent = settings.enabled ? 'Автообработка включена.' : 'Автообработка приостановлена.';
    else if (event) status.textContent = 'Интервал сохранён.';
  } catch (error) {
    scheduleForm.hidden = false;
    if (toggleOnly) enabled.checked = enabled.defaultChecked;
    status.textContent = error.message;
    status.classList.add('error-text');
  } finally {
    controls.disabled = false;
  }
}

/** Перечитывает Excel через API и обновляет таблицу прайса. */
async function loadPrice() {
  const button = byId('price-refresh');
  const rows = byId('price-rows');
  const message = byId('price-message');
  const count = byId('price-count');
  button.disabled = true;
  rows.replaceChildren();
  count.hidden = true;
  message.hidden = false;
  message.classList.remove('error-text');
  message.textContent = 'Загружаем прайс-лист…';
  try {
    const { items } = await read('/api/price');
    rows.replaceChildren(...items.map((item) => {
      // Одна строка таблицы соответствует одной записи Excel.
      const row = element('tr');
      for (const value of [item.sku, item.name, item.description || '–', item.synonyms.join('; ') || '–', item.unit, money(item.price)]) {
        row.insertCell().textContent = value;
      }
      return row;
    }));
    count.textContent = `Позиций: ${items.length}`;
    count.hidden = false;
    message.hidden = true;
  } catch (error) {
    message.textContent = error.message;
    message.classList.add('error-text');
  } finally {
    button.disabled = false;
  }
}

/** Загружает страницу технических событий с учётом фильтра ошибок. */
async function loadEvents(targetPage = 1) {
  eventsRequest?.abort();
  const request = new AbortController();
  eventsRequest = request;
  const onlyErrors = byId('only-errors').checked;
  const rows = byId('events-rows');
  const message = byId('events-message');
  const query = new URLSearchParams({ page: targetPage, errors: onlyErrors ? '1' : '0' });
  try {
    const data = await read(`/api/events?${query}`, request.signal);
    rows.replaceChildren(...data.items.map((item) => {
      // Код ошибки выводится в исходном виде для поиска в техническом журнале.
      const row = element('tr');
      for (const value of [eventTimeFormat.format(new Date(item.occurred_at)), item.request_id || '–',
        eventStages[item.stage] || item.stage, statusPill(item.status, eventStatuses[item.status]), item.error_code || '–']) {
        row.insertCell().append(value);
      }
      return row;
    }));
    eventsPage = data.page;
    updatePagination(data, 'events-', '0 событий');
    message.hidden = Boolean(data.items.length);
    message.classList.remove('error-text');
    if (!data.ready) message.textContent = 'События появятся после запуска робота.';
    else message.textContent = onlyErrors ? 'Ошибок в журнале нет.' : 'Пока нет событий.';
  } catch (error) {
    if (request.signal.aborted) return;
    rows.replaceChildren();
    byId('events-range').textContent = '';
    byId('events-previous').disabled = byId('events-next').disabled = true;
    message.hidden = false;
    message.classList.add('error-text');
    message.textContent = error.message;
  }
}

/** Запускает цикл по запросу и опрашивает состояние до его завершения. */
async function checkRun(method = 'GET') {
  runButton.disabled = true;
  try {
    const { state, output, note } = await read('/api/run', undefined, method);
    const status = {
      running: 'Робот обрабатывает почту…', succeeded: 'Обработка завершена.',
      failed: 'Цикл обработки остановлен. Причина указана в отчёте.', disabled: note,
    }[state] || '';
    const running = state === 'running';
    if (runStatus.textContent !== status) runStatus.textContent = status;
    byId('run-info').hidden = !status;
    runStatus.classList.toggle('error-text', state === 'failed');
    byId('run-output').textContent = output;
    runLog.hidden = !output;
    if (running) runLog.open = false;
    runButton.disabled = running || state === 'disabled';
    if (running) setTimeout(checkRun, 1000);
    else if (lastRun === 'running') loadList(page);
    lastRun = state;
  } catch (error) {
    byId('run-info').hidden = false;
    runStatus.classList.add('error-text');
    runStatus.textContent = error.message;
    if (lastRun === 'running') setTimeout(checkRun, 2000);
    else runButton.disabled = false;
  }
}

/** Показывает пустое состояние, загрузку или ошибку в журнале обращений. */
function listMessage(text, { title = '', error = false } = {}) {
  const message = byId('list-message');
  const heading = byId('list-title');
  heading.textContent = title;
  heading.hidden = !title;
  byId('list-text').textContent = text;
  message.hidden = !text;
  message.classList.toggle('error-text', error);
}

/** Обновляет диапазон строк и кнопки перехода для выбранного журнала. */
function updatePagination(data, prefix, empty) {
  const first = (data.page - 1) * data.page_size + 1;
  const last = first + data.items.length - 1;
  byId(`${prefix}range`).textContent = data.total ? `${first}–${last} из ${data.total}` : empty;
  byId(`${prefix}previous`).disabled = data.page <= 1;
  byId(`${prefix}next`).disabled = last >= data.total;
}

/** Выводит обращения, счётчики и выбранную страницу журнала. */
function renderList(data) {
  for (const counter of document.querySelectorAll('[data-count]')) {
    counter.textContent = data.counts[counter.dataset.count];
  }
  byId('rows').replaceChildren(...data.items.map((item) => {
    // Кнопка с темой письма открывает карточку именно этого обращения.
    const row = element('tr');
    const button = element('button', subject(item), 'subject');
    button.type = 'button';
    button.addEventListener('click', () => openDetail(item.id));
    for (const value of [button, item.reply_to || '—', statusPill(item.status), responseType(item) || '—', money(item.total), dateText(item.updated_at)]) {
      row.insertCell().append(value);
    }
    return row;
  }));
  updatePagination(data, '', '0 обращений');
  page = data.page;
  if (!data.ready) listMessage('Запустите робота и обновите журнал.', { title: 'База ещё не создана' });
  else if (data.items.length) listMessage('');
  else if (data.counts.all) listMessage('Измените фильтры или условия поиска.', { title: 'Обращения не найдены' });
  else listMessage('Новые письма появятся здесь после обработки.', { title: 'Пока нет обращений' });
}

/** Запрашивает страницу обращений с текущими поиском и фильтрами. */
async function loadList(targetPage = 1) {
  clearTimeout(searchTimer);
  // Отменённый запрос не должен перерисовать журнал после смены фильтра.
  listRequest?.abort();
  const request = new AbortController();
  listRequest = request;
  const query = new URLSearchParams(new FormData(filters));
  query.set('page', targetPage);
  const hasPeriod = Boolean(query.get('from') || query.get('to'));
  periodTrigger.classList.toggle('is-filtered', hasPeriod);
  byId('reset').hidden = !(hasPeriod || statusFilter.value || query.get('q').trim());
  for (const tab of statusTabs) tab.setAttribute('aria-pressed', tab.dataset.status === statusFilter.value);
  try {
    renderList(await read(`/api/requests?${query}`, request.signal));
  } catch (error) {
    if (request.signal.aborted) return;
    byId('rows').replaceChildren();
    byId('range').textContent = '';
    byId('previous').disabled = byId('next').disabled = true;
    listMessage(error.message, { error: true });
  }
}

/** Формирует список подписей и непустых значений для карточки. */
function definitionList(fields) {
  const list = element('dl', null, 'metadata');
  for (const [label, value] of fields) {
    if (!value) continue;
    const field = element('div');
    field.append(element('dt', label), element('dd', value));
    list.append(field);
  }
  return list;
}

/** Оборачивает часть карточки в секцию с заголовком. */
function section(title, content) {
  const part = element('section', null, 'detail-section');
  part.append(element('h3', title), content);
  return part;
}

/** Создаёт раскрывающийся блок с текстом письма. */
function messagePanel(label, text) {
  const panel = element('details', null, 'detail-section');
  panel.append(element('summary', label));
  if (text) panel.append(element('pre', text, 'message-text'));
  return panel;
}

/** Показывает позиции расчёта с количеством, ценой и суммой. */
function orderTable(lines) {
  const wrapper = element('div', null, 'table-wrap');
  const table = element('table');
  const header = table.createTHead().insertRow();
  for (const title of ['Товар', 'Кол-во', 'Цена', 'Сумма']) {
    const heading = element('th', title);
    heading.scope = 'col';
    header.append(heading);
  }
  const body = table.createTBody();
  for (const line of lines) {
    const row = body.insertRow();
    for (const value of [`${line.name} · ${line.sku}`, `${line.quantity} ${line.unit}`, money(line.price), money(line.amount)]) {
      row.insertCell().textContent = value;
    }
  }
  wrapper.append(table);
  return wrapper;
}

/** Заполняет карточку статусом, этапами, расчётом и сохранённым ответом. */
function renderDetail(item) {
  detailBody.replaceChildren(statusPill(item.status), element('h3', subject(item), 'detail-subject'), definitionList([
    ['Адрес для ответа', item.reply_to], ['Ответ', responseType(item)],
    ['Сумма', item.total == null ? '' : money(item.total)], ['ID обращения', item.id],
  ]));
  const times = [['Статус обновлён', dateText(item.updated_at)]];
  if (item.analyzed_at) times.unshift(['Заказ обработан', dateText(item.analyzed_at)]);
  if (item.created_at) times.splice(times.length - 1, 0, ['Ответ сформирован', dateText(item.created_at)]);
  times.push(['Версия прайса · SHA-256', item.price_sha256]);
  detailBody.append(section('Ход обработки', definitionList(times)));
  if (item.issues.length) {
    const issues = element('ul');
    for (const issue of item.issues) {
      issues.append(element('li', issue.fragment ? `${issue.message} · «${issue.fragment}»` : issue.message));
    }
    detailBody.append(section('Причины и уточнения', issues));
  }
  if (item.lines.length) detailBody.append(section('Позиции заказа', orderTable(item.lines)));
  if (item.body) detailBody.append(messagePanel('Входящее письмо', item.body));
  if (item.archive) {
    const panel = messagePanel('Копия ответа', item.archive.text);
    const link = element('a', 'Скачать .eml', 'button');
    link.href = `/api/archive?${new URLSearchParams({ id: item.id })}`;
    link.download = item.archive.name;
    panel.append(link);
    detailBody.append(panel);
  }
}

/** Открывает карточку и загружает данные обращения по его ID. */
async function openDetail(id) {
  detailRequest?.abort();
  const request = new AbortController();
  detailRequest = request;
  detailBody.replaceChildren(element('p', 'Загружаем карточку…'));
  detailBody.scrollTop = 0;
  detail.showModal();
  try {
    const item = await read(`/api/detail?${new URLSearchParams({ id })}`, request.signal);
    if (detail.open) renderDetail(item);
  } catch (error) {
    if (!request.signal.aborted) detailBody.replaceChildren(element('p', error.message, 'error-text'));
  }
}

// Закрытие карточки отменяет незавершённое чтение её содержимого.
detail.addEventListener('close', () => detailRequest?.abort());
// Нажатие Enter применяет фильтры без перехода на другую страницу.
filters.addEventListener('submit', (event) => { event.preventDefault(); loadList(); });
// Поля сбрасывает браузер после события reset. Запрашиваем список следующим шагом.
filters.addEventListener('reset', () => { period.open = false; setTimeout(loadList, 0); });
// Поиск обновляется с задержкой ниже, остальные фильтры применяются сразу.
filters.addEventListener('change', ({ target }) => { if (target.name !== 'q') loadList(); });
// Меню периода закрывается при клике за его пределами.
document.addEventListener('click', ({ target }) => { if (!period.contains(target)) period.open = false; });
period.addEventListener('keydown', (event) => {
  // Escape закрывает меню периода и возвращает фокус на кнопку.
  if (event.key !== 'Escape' || !period.open || event.defaultPrevented) return;
  period.open = false;
  periodTrigger.focus();
});
filters.elements.namedItem('q').addEventListener('input', () => {
  // Пауза позволяет выполнить один поиск после серии нажатий клавиш.
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadList, 200);
});
for (const tab of statusTabs) {
  // Быстрая вкладка меняет то же поле статуса, что и выпадающий список.
  tab.addEventListener('click', () => { statusFilter.value = tab.dataset.status; loadList(); });
}
// Переходы и обновление журнала используют текущие фильтры.
byId('refresh').addEventListener('click', () => loadList(page));
byId('previous').addEventListener('click', () => loadList(page - 1));
byId('next').addEventListener('click', () => loadList(page + 1));
// Только POST запускает цикл, а последующие опросы читают его состояние.
runButton.addEventListener('click', () => checkRun('POST'));
scheduleForm.addEventListener('submit', updateSchedule);
byId('schedule-enabled').addEventListener('change', updateSchedule);
// Все вкладки используют одну HTML-страницу, а маршрут определяет видимый раздел.
const view = { '/price': 'price', '/technical-journal': 'events' }[location.pathname];
if (view) {
  const main = byId(`${view}-view`);
  document.title = `${main.querySelector('h1').textContent} – ОфисСнаб`;
  byId('journal-tab').removeAttribute('aria-current');
  byId(`${view}-tab`).setAttribute('aria-current', 'page');
  byId('journal-view').hidden = true;
  main.hidden = false;
}
if (view === 'price') {
  byId('price-refresh').addEventListener('click', loadPrice);
  loadPrice();
} else if (view === 'events') {
  // При смене фильтра открывается первая страница, кнопки сохраняют выбранный фильтр.
  byId('only-errors').addEventListener('change', () => loadEvents());
  byId('events-refresh').addEventListener('click', () => loadEvents(eventsPage));
  byId('events-previous').addEventListener('click', () => loadEvents(eventsPage - 1));
  byId('events-next').addEventListener('click', () => loadEvents(eventsPage + 1));
  loadEvents();
} else {
  loadList();
  checkRun();
  updateSchedule();
}
