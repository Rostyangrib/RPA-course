'use strict';

const byId = (id) => document.getElementById(id);
const filters = byId('filters');
const statusFilter = filters.elements.namedItem('status');
const statusTabs = document.querySelectorAll('[data-status]');
const period = byId('period');
const periodTrigger = byId('period-trigger');
const statuses = Object.fromEntries([...statusFilter.options].map(({ value, text }) => [value, text]));
const detail = byId('detail');
const detailBody = byId('detail-body');
const runButton = byId('run-start');
const runStatus = byId('run-status');
const runLog = byId('run-log');
const timeFormat = new Intl.DateTimeFormat('ru-RU', {
  timeZone: 'Asia/Irkutsk', dateStyle: 'short', timeStyle: 'short',
});
const moneyFormat = new Intl.NumberFormat('ru-RU', { style: 'currency', currency: 'RUB' });
const dateText = (value) => value ? timeFormat.format(new Date(value)) : '—';
const money = (value) => value == null ? '—' : moneyFormat.format(value);
const subject = (item) => item.subject || (item.status === 'skipped' ? 'Пропущенное письмо' : 'Без темы');
const responseType = (item) => item.quote_number || { quote: 'КП', clarification: 'Уточнение' }[item.outcome];
let page = 1;
let searchTimer;
let listRequest;
let detailRequest;
let lastRun;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function statusPill(status) {
  return element('span', statuses[status] || status, `pill ${status}`);
}

async function read(url, signal, method = 'GET') {
  const response = await fetch(url, { signal, method });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || data.note || 'Не удалось загрузить данные.');
  return data;
}

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

function listMessage(text, { title = '', error = false } = {}) {
  const message = byId('list-message');
  const heading = byId('list-title');
  heading.textContent = title;
  heading.hidden = !title;
  byId('list-text').textContent = text;
  message.hidden = !text;
  message.classList.toggle('error-text', error);
}

function renderList(data) {
  for (const counter of document.querySelectorAll('[data-count]')) {
    counter.textContent = data.counts[counter.dataset.count];
  }
  byId('rows').replaceChildren(...data.items.map((item) => {
    const row = element('tr');
    const button = element('button', subject(item), 'subject');
    button.type = 'button';
    button.addEventListener('click', () => openDetail(item.id));
    for (const value of [button, item.reply_to || '—', statusPill(item.status), responseType(item) || '—', money(item.total), dateText(item.updated_at)]) {
      row.insertCell().append(value);
    }
    return row;
  }));
  const first = (data.page - 1) * data.page_size + 1;
  byId('range').textContent = data.total ? `${first}–${first + data.items.length - 1} из ${data.total}` : '0 обращений';
  byId('previous').disabled = data.page <= 1;
  byId('next').disabled = first + data.items.length - 1 >= data.total;
  page = data.page;
  if (!data.ready) listMessage('Запустите робота и обновите журнал.', { title: 'База ещё не создана' });
  else if (data.items.length) listMessage('');
  else if (data.counts.all) listMessage('Измените фильтры или условия поиска.', { title: 'Обращения не найдены' });
  else listMessage('Новые письма появятся здесь после обработки.', { title: 'Пока нет обращений' });
}

async function loadList(targetPage = 1) {
  clearTimeout(searchTimer);
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

function section(title, content) {
  const part = element('section', null, 'detail-section');
  part.append(element('h3', title), content);
  return part;
}

function messagePanel(label, text) {
  const panel = element('details', null, 'detail-section');
  panel.append(element('summary', label));
  if (text) panel.append(element('pre', text, 'message-text'));
  return panel;
}

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

detail.addEventListener('close', () => detailRequest?.abort());
filters.addEventListener('submit', (event) => { event.preventDefault(); loadList(); });
filters.addEventListener('reset', () => { period.open = false; setTimeout(loadList, 0); });
filters.addEventListener('change', ({ target }) => { if (target.name !== 'q') loadList(); });
document.addEventListener('click', ({ target }) => { if (!period.contains(target)) period.open = false; });
period.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape' || !period.open || event.defaultPrevented) return;
  period.open = false;
  periodTrigger.focus();
});
filters.elements.namedItem('q').addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadList, 200);
});
for (const tab of statusTabs) {
  tab.addEventListener('click', () => { statusFilter.value = tab.dataset.status; loadList(); });
}
byId('refresh').addEventListener('click', () => loadList(page));
byId('previous').addEventListener('click', () => loadList(page - 1));
byId('next').addEventListener('click', () => loadList(page + 1));
runButton.addEventListener('click', () => checkRun('POST'));
loadList();
checkRun();
