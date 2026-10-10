import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Activity, ArrowDown, ArrowDownUp, ArrowRight, ArrowUpRight, Bookmark, Check, ChevronDown, ChevronLeft, ChevronRight, CircleHelp, Clock3, Download, ExternalLink, Filter, Layers3, ListFilter, LoaderCircle, Pause, Play, RefreshCw, Search, ShieldCheck, SlidersHorizontal, Sparkles, TrendingUp, X } from 'lucide-react'
import type { Opportunity, Snapshot, SourceStatus } from './types'

type Currency = 'RUB' | 'USD'
type Sort = 'roi' | 'profit_usd' | 'cost_usd' | 'updated_at'
const number = new Intl.NumberFormat('ru-RU')
const date = (stamp: number | null | undefined) => stamp ? new Date(stamp * 1000).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—'
const readSaved = <T,>(key: string, fallback: T): T => {
  try { return JSON.parse(localStorage.getItem(key) ?? 'null') ?? fallback } catch { return fallback }
}
const ageText = (age: number) => age < 60 ? `${Math.max(0, Math.floor(age))} с` : `${Math.floor(age / 60)} мин`

function SourceDot({ status }: { status?: SourceStatus }) {
  return <span className={`status-dot ${status?.state ?? 'loading'}`} />
}

export default function App() {
  const [data, setData] = useState<Snapshot | null>(null)
  const [receivedAt, setReceivedAt] = useState(0)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [paused, setPaused] = useState(false)
  const [clock, setClock] = useState(Date.now())
  const [currency, setCurrency] = useState<Currency>(() => readSaved('dota-currency', 'USD'))
  const [favorites, setFavorites] = useState<string[]>(() => readSaved('dota-favorites', []))
  const [view, setView] = useState<'scanner' | 'favorites'>('scanner')
  const [query, setQuery] = useState('')
  const [minRoi, setMinRoi] = useState('')
  const [minPrice, setMinPrice] = useState('')
  const [maxPrice, setMaxPrice] = useState('')
  const [minQuantity, setMinQuantity] = useState('')
  const [sort, setSort] = useState<Sort>('roi')
  const [page, setPage] = useState(1)
  const [modal, setModal] = useState<'settings' | 'help' | null>(null)
  const [depositFee, setDepositFee] = useState<number>(() => readSaved('dota-deposit-fee', 0))
  const [draftFee, setDraftFee] = useState('0')
  const [feeError, setFeeError] = useState('')
  const [selected, setSelected] = useState<Opportunity | null>(null)
  const inFlight = useRef(false)
  const abort = useRef<AbortController | null>(null)
  const requestGeneration = useRef(0)
  const nextFetch = useRef(0)
  const [nextAt, setNextAt] = useState(0)

  const fetchData = useCallback(async () => {
    if (inFlight.current) return
    inFlight.current = true
    const generation = requestGeneration.current
    const controller = new AbortController()
    abort.current = controller
    setLoading(true)
    const timeout = window.setTimeout(() => controller.abort(), 8000)
    try {
      const response = await fetch(`/api/opportunities?deposit_fee=${depositFee}`, { signal: controller.signal })
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      const snapshot = await response.json() as Snapshot
      if (generation !== requestGeneration.current) return
      setData(snapshot)
      setReceivedAt(Date.now())
      setError('')
    } catch (err) {
      if (generation === requestGeneration.current) setError('Не удалось обновить данные. Проверяем соединение с сервером.')
    } finally {
      window.clearTimeout(timeout)
      if (generation === requestGeneration.current) {
        inFlight.current = false
        setLoading(false)
        nextFetch.current = Date.now() + 10000
        setNextAt(nextFetch.current)
      }
    }
  }, [depositFee])

  useEffect(() => {
    requestGeneration.current += 1
    inFlight.current = false
    void fetchData()
    return () => { requestGeneration.current += 1; abort.current?.abort(); inFlight.current = false }
  }, [fetchData])

  useEffect(() => {
    const tick = window.setInterval(() => setClock(Date.now()), 1000)
    const poll = window.setInterval(() => {
      if (!paused && !document.hidden) void fetchData()
    }, 10000)
    const visible = () => { if (!document.hidden && !paused) void fetchData() }
    document.addEventListener('visibilitychange', visible)
    return () => { window.clearInterval(tick); window.clearInterval(poll); document.removeEventListener('visibilitychange', visible) }
  }, [fetchData, paused])

  useEffect(() => { localStorage.setItem('dota-currency', JSON.stringify(currency)) }, [currency])
  useEffect(() => { localStorage.setItem('dota-favorites', JSON.stringify(favorites)) }, [favorites])
  useEffect(() => { localStorage.setItem('dota-deposit-fee', JSON.stringify(depositFee)) }, [depositFee])
  useEffect(() => { setPage(1) }, [query, minRoi, minPrice, maxPrice, minQuantity, sort, view, currency])
  useEffect(() => {
    if (!modal && !selected) return
    const previousFocus = document.activeElement as HTMLElement | null
    const dialog = document.querySelector<HTMLElement>('.modal')
    const targets = () => Array.from(dialog?.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], input:not([disabled]), select:not([disabled])') ?? [])
    if (modal !== 'settings') targets()[0]?.focus()
    const close = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { setModal(null); setSelected(null) }
      if (event.key === 'Tab') {
        const nodes = targets()
        if (event.shiftKey && document.activeElement === nodes[0]) { event.preventDefault(); nodes.at(-1)?.focus() }
        else if (!event.shiftKey && document.activeElement === nodes.at(-1)) { event.preventDefault(); nodes[0]?.focus() }
      }
    }
    document.addEventListener('keydown', close)
    return () => { document.removeEventListener('keydown', close); previousFocus?.focus() }
  }, [modal, selected])

  const serverNow = (data?.server_time ?? clock / 1000) + (receivedAt ? (clock - receivedAt) / 1000 : 0)
  const fx = data?.fx ? Number(data.fx.rub_per_usd) : null
  const multiplier = currency === 'USD' ? 1 : fx
  const money = (value: number | null | undefined) => value == null || multiplier == null ? '—' : new Intl.NumberFormat('ru-RU', { style: 'currency', currency, maximumFractionDigits: 2 }).format(value * multiplier)
  const isFresh = (item: Opportunity) => data && serverNow - item.updated_at <= data.quote_ttl_seconds && data.catalog_updated_at && serverNow - data.catalog_updated_at <= 180 && data.fx && serverNow - data.fx.checked_at <= 86400 * 3
  const freshItems = useMemo(() => (data?.items ?? []).filter(isFresh), [data, clock, receivedAt])
  const filtered = useMemo(() => {
    return freshItems.filter(item => {
      const cost = item.cost_usd * (multiplier ?? 0)
      return (!query || item.name.toLowerCase().includes(query.trim().toLowerCase()))
        && (view === 'scanner' || favorites.includes(item.name))
        && (!minRoi || item.roi >= Number(minRoi))
        && (!minPrice || (multiplier !== null && cost >= Number(minPrice)))
        && (!maxPrice || (multiplier !== null && cost <= Number(maxPrice)))
        && (!minQuantity || item.buy_order_quantity >= Number(minQuantity))
    }).sort((a, b) => sort === 'cost_usd' ? a[sort] - b[sort] : b[sort] - a[sort])
  }, [freshItems, query, view, favorites, minRoi, minPrice, maxPrice, minQuantity, sort, multiplier])
  const pages = Math.max(1, Math.ceil(filtered.length / 15))
  const actualPage = Math.min(page, pages)
  const visible = filtered.slice((actualPage - 1) * 15, actualPage * 15)
  const bestRoi = freshItems.length ? Math.max(...freshItems.map(item => item.roi)) : null
  const countdown = Math.max(0, Math.min(10, Math.ceil((nextAt - clock) / 1000)))
  const hasFilters = Boolean(query || minRoi || minPrice || maxPrice || minQuantity)
  const toggleFavorite = (name: string) => setFavorites(old => old.includes(name) ? old.filter(value => value !== name) : [...old, name])
  const clearFilters = () => { setQuery(''); setMinRoi(''); setMinPrice(''); setMaxPrice(''); setMinQuantity('') }

  const exportCsv = () => {
    const headings = ['Предмет', 'Валюта отображения', 'Стоимость покупки', 'Ордер Steam', 'На баланс Steam', 'Выгода', 'Выгода %', 'Количество по ордеру', 'Валюта ордера', 'Ордер в исходной валюте', 'Дата котировки', 'Steam', 'Lis-Skins']
    const rows = filtered.map(item => [item.name, currency, multiplier == null ? '' : item.cost_usd * multiplier, multiplier == null ? '' : item.steam_bid_usd * multiplier, multiplier == null ? '' : item.steam_net_usd * multiplier, multiplier == null ? '' : item.profit_usd * multiplier, item.roi, item.buy_order_quantity, item.source_currency, item.source_bid, new Date(item.updated_at * 1000).toISOString(), item.steam_url, item.lis_url])
    const escape = (value: unknown) => { const text = String(value); return `"${(/^[=+@\-]/.test(text) ? "'" : '') + text.replaceAll('"', '""')}"` }
    const blob = new Blob(['\uFEFF' + [headings, ...rows].map(row => row.map(escape).join(';')).join('\r\n')], { type: 'text/csv;charset=utf-8' })
    const link = document.createElement('a'); const url = URL.createObjectURL(blob)
    link.href = url; link.download = `dota-spread-${new Date().toISOString().slice(0, 10)}.csv`; link.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 500)
  }

  const openSettings = () => { setDraftFee(String(depositFee)); setFeeError(''); setModal('settings') }
  const saveSettings = () => {
    const fee = Number(draftFee.replace(',', '.'))
    if (!Number.isFinite(fee) || fee < 0 || fee > 100) { setFeeError('Введите число от 0 до 100.'); return }
    setDepositFee(fee); setModal(null)
  }

  return <div className="app-shell">
    <aside className="sidebar">
      <a className="brand" href="/" aria-label="Dota Spread, главная"><span className="brand-mark"><span /></span><span>DOTA<span className="brand-light">SPREAD</span><small>PRICE INTELLIGENCE</small></span></a>
      <div className="workspace-label">РАБОЧЕЕ ПРОСТРАНСТВО</div>
      <nav className="side-nav" aria-label="Основная навигация">
        <button className={view === 'scanner' ? 'active' : ''} onClick={() => setView('scanner')}><Layers3 size={18} />Сканер рынка<span className="nav-dot" /></button>
        <button className={view === 'favorites' ? 'active' : ''} onClick={() => setView('favorites')}><Bookmark size={18} />Избранное{favorites.length > 0 && <span className="nav-count">{favorites.length}</span>}</button>
      </nav>
      <div className="game-card"><span className="dota-mark">D</span><div><strong>Dota 2</strong><span>Выбранная игра</span></div><Check size={15} /></div>
      <div className="sidebar-bottom">
        <div className="source-heading">ИСТОЧНИКИ ЦЕН <Activity size={13} /></div>
        <div className="source-line"><span className="lis-symbol">L</span><span>Lis-Skins<small>{data?.catalog_total ? `${number.format(data.catalog_total)} предметов` : 'Загрузка каталога'}</small></span><SourceDot status={data?.sources.lis} /></div>
        <div className="source-line"><span className="steam-symbol">S</span><span>Steam Market<small>{data?.checked_total ? `${number.format(data.checked_total)} проверено` : 'Получение ордеров'}</small></span><SourceDot status={data?.sources.steam} /></div>
        <div className="sidebar-divider" />
        <button className="help-link" onClick={() => setModal('help')}><CircleHelp size={17} />Как считается выгода<ArrowUpRight size={14} /></button>
        <div className="sidebar-footer"><span className="status-dot ok" /> Dota Spread <span>v0.1</span></div>
      </div>
    </aside>

    <main>
      <header className="topbar"><div className="breadcrumb">Рабочее пространство<ChevronRight size={13} /><span>{view === 'scanner' ? 'Сканер Dota 2' : 'Избранное'}</span></div><div className="topbar-actions"><span className="live-status"><span className={`status-dot ${paused || error ? 'stale' : 'ok'}`} />{paused ? 'На паузе' : 'Мониторинг активен'}</span><button className="icon-button" aria-label="Настройки расчёта" onClick={openSettings}><SlidersHorizontal size={17} /></button></div></header>
      <div className="main-content">
        <section className="page-heading"><div><div className="eyebrow"><span /> LIS-SKINS <ArrowRight size={12} /> STEAM MARKET</div><h1>{view === 'scanner' ? 'Выгодные скины' : 'Избранные скины'}<span className="game-tag">DOTA 2</span></h1><p>Находи разницу в ценах. Комиссия Steam уже учтена.</p></div><div className="heading-actions"><div className="currency-switch" role="group" aria-label="Валюта цен"><button onClick={() => setCurrency('USD')} className={currency === 'USD' ? 'selected' : ''}>$ USD</button><button onClick={() => setCurrency('RUB')} className={currency === 'RUB' ? 'selected' : ''}>₽ RUB</button></div><button className="button secondary" onClick={exportCsv} disabled={!filtered.length}><Download size={15} />Экспорт</button></div></section>

        <section className="stats-grid" aria-label="Обзор рынка">
          <article className="stat-card"><div className="stat-label">Выгодных позиций<span className="stat-icon green"><TrendingUp size={17} /></span></div><div className="stat-number">{data ? number.format(freshItems.length) : '—'}<span className="tiny-label">сейчас</span></div><div className="stat-caption"><span className="mini-dot" />После комиссии Steam</div></article>
          <article className="stat-card"><div className="stat-label">Лучшая выгода<span className="stat-icon green"><Sparkles size={17} /></span></div><div className="stat-number accent">{bestRoi !== null ? `+${bestRoi.toFixed(1)}%` : '—'}</div><div className="stat-caption">К стоимости покупки</div></article>
          <article className="stat-card"><div className="stat-label">Свежих котировок<span className="stat-icon"><Activity size={17} /></span></div><div className="stat-number">{data ? number.format(data.fresh_quotes) : '—'}</div><div className="stat-caption">Ордера не старше {data?.quote_ttl_seconds ?? 120} сек.</div></article>
          <article className="stat-card"><div className="stat-label">Проверено предметов<span className="stat-icon"><Layers3 size={17} /></span></div><div className="stat-number">{data ? number.format(data.checked_total) : '—'}<span className="stat-total">/ {data?.catalog_total ? number.format(data.catalog_total) : '…'}</span></div><div className="progress-track"><span style={{ width: `${data?.catalog_total ? Math.min(100, data.checked_total / data.catalog_total * 100) : 0}%` }} /></div></article>
        </section>

        {error && <div className="notice error-notice" role="alert"><Activity size={16} />{error}<button onClick={() => void fetchData()}>Повторить</button></div>}
        {data && Object.entries(data.sources).some(([, status]) => status.state === 'error' || status.state === 'stale') && <div className="notice source-notice"><Clock3 size={16} /><div>{Object.entries(data.sources).filter(([, status]) => status.state === 'error' || status.state === 'stale').map(([key, status]) => <span key={key}>{key === 'lis' ? 'Lis-Skins' : key === 'fx' ? 'Курс валют' : 'Steam'}: {status.message} </span>)}</div></div>}

        <section className="market-panel">
          <div className="panel-heading"><div className="panel-tabs"><span className="panel-tab"><ListFilter size={17} />{view === 'scanner' ? 'Все выгодные позиции' : 'В избранном'}<span>{filtered.length}</span></span></div><div className="refresh-control"><span><Clock3 size={13} />{paused ? 'Обновление на паузе' : `Обновление через ${countdown} с`}</span><button className="icon-button" title={paused ? 'Продолжить обновление' : 'Приостановить обновление экрана'} aria-label={paused ? 'Продолжить обновление' : 'Приостановить обновление'} onClick={() => { setPaused(!paused); if (paused) void fetchData() }}>{paused ? <Play size={14} /> : <Pause size={14} />}</button><button className="icon-button" aria-label="Обновить сейчас" onClick={() => void fetchData()} disabled={loading}><RefreshCw size={14} className={loading ? 'spin' : ''} /></button></div></div>
          <div className="filters"><label className="search-box"><Search size={17} /><input placeholder="Найти скин по названию…" aria-label="Поиск по названию" value={query} onChange={event => setQuery(event.target.value)} />{query && <button className="icon-button" aria-label="Очистить поиск" onClick={() => setQuery('')}><X size={14} /></button>}</label><label className="filter-input"><span>Выгода от</span><input type="number" min="0" placeholder="0" aria-label="Минимальная выгода в процентах" value={minRoi} onChange={event => setMinRoi(event.target.value)} /><span>%</span></label><label className="filter-input price-filter"><span>Цена</span><input type="number" min="0" placeholder="от" aria-label="Минимальная цена" value={minPrice} onChange={event => setMinPrice(event.target.value)} /><span>—</span><input type="number" min="0" placeholder="до" aria-label="Максимальная цена" value={maxPrice} onChange={event => setMaxPrice(event.target.value)} /><span>{currency === 'USD' ? '$' : '₽'}</span></label><button className={`button filter-button ${modal === 'settings' ? 'active' : ''}`} onClick={openSettings}><SlidersHorizontal size={15} />Расчёт{depositFee > 0 && <span className="filter-badge">1</span>}</button></div>
          <div className="table-toolbar"><div className="positive-badge"><span className="mini-dot" />Только положительная выгода</div><div className="toolbar-right">{view === 'favorites' && favorites.length > 0 && <button className="reset-button" onClick={() => setFavorites([])}>Очистить избранное<X size={12} /></button>}{hasFilters && <button className="reset-button" onClick={clearFilters}>Сбросить фильтры<X size={12} /></button>}<label className="quantity-filter"><span>Ордеров от</span><input type="number" min="1" placeholder="1" aria-label="Минимальное количество по лучшему ордеру" value={minQuantity} onChange={event => setMinQuantity(event.target.value)} /></label><label className="sort-select"><ArrowDownUp size={13} /><select aria-label="Сортировка позиций" value={sort} onChange={event => setSort(event.target.value as Sort)}><option value="roi">По выгоде, %</option><option value="profit_usd">По выгоде, сумма</option><option value="cost_usd">По цене покупки</option><option value="updated_at">По свежести</option></select><ChevronDown size={12} /></label></div></div>
          <div className="table-scroll"><table><thead><tr><th className="star-column"><Bookmark size={13} /></th><th>Предмет</th><th><button onClick={() => setSort('cost_usd')}>Lis-Skins{sort === 'cost_usd' && <ArrowDown size={11} />}</button></th><th>Ордер Steam</th><th title="Сумма за вычетом обеих комиссий Steam">На баланс Steam</th><th><button onClick={() => setSort('profit_usd')}>Выгода{sort === 'profit_usd' && <ArrowDown size={11} />}</button></th><th><button className={sort === 'roi' ? 'active-sort' : ''} onClick={() => setSort('roi')}>Выгода, %{sort === 'roi' && <ArrowDown size={11} />}</button></th><th>Свежесть</th><th /></tr></thead><tbody>{visible.map(item => <tr key={item.name}><td className="star-column"><button className={`favorite-button ${favorites.includes(item.name) ? 'saved' : ''}`} aria-label={`${favorites.includes(item.name) ? 'Убрать из избранного' : 'Добавить в избранное'}: ${item.name}`} aria-pressed={favorites.includes(item.name)} onClick={() => toggleFavorite(item.name)}><Bookmark size={15} fill={favorites.includes(item.name) ? 'currentColor' : 'none'} /></button></td><td className="item-column"><button className="item-button" onClick={() => setSelected(item)}><span className="item-image">{item.image ? <img src={item.image} alt="" loading="lazy" onError={event => { event.currentTarget.style.display = 'none' }} /> : <Layers3 size={23} />}</span><span><strong>{item.name}</strong><small>Dota 2<span>·</span>{number.format(item.stock)} в наличии</small></span></button></td><td className="money-cell">{money(item.cost_usd)}{depositFee > 0 && <small>с пополнением +{depositFee}%</small>}</td><td className="money-cell" title={`Исходный ордер: ${item.source_bid} ${item.source_currency}. ${item.buy_order_quantity} шт. по этой цене.`}>{money(item.steam_bid_usd)}<small>{item.buy_order_quantity} шт. по этой цене</small></td><td className="money-cell net-cell">{money(item.steam_net_usd)}<small>после комиссии</small></td><td className="money-cell profit-cell">+{money(item.profit_usd)}</td><td><span className="roi-badge">+{item.roi.toFixed(2)}%<TrendingUp size={12} /></span></td><td><span className="freshness"><span className="mini-dot" />{ageText(serverNow - item.updated_at)}</span></td><td><a className="row-action" href={item.lis_url} target="_blank" rel="noopener noreferrer" title="Найти предмет на Lis-Skins" aria-label={`Найти ${item.name} на Lis-Skins`}><ArrowUpRight size={18} /></a></td></tr>)}</tbody></table></div>
          {!visible.length && <div className="empty-state"><div className="empty-visual">{!data || (data.checked_total === 0 && data.collecting) ? <LoaderCircle className="spin" size={27} /> : view === 'favorites' ? <Bookmark size={26} /> : hasFilters ? <Filter size={26} /> : <Search size={27} />}</div><h2>{!data || (data.checked_total === 0 && data.collecting) ? 'Собираем первые котировки' : view === 'favorites' ? (favorites.length ? 'Сейчас нет подходящих избранных сделок' : 'Здесь будут избранные сделки') : hasFilters ? 'По этим фильтрам ничего не найдено' : 'Ждём выгодную разницу'}</h2><p>{!data || (data.checked_total === 0 && data.collecting) ? 'Загружаем каталог Lis-Skins и проверяем ордера Steam. Первые результаты появятся автоматически.' : view === 'favorites' ? 'Сохрани скин закладкой в таблице. Здесь появятся его актуальные выгодные предложения.' : hasFilters ? 'Попробуй уменьшить минимальную выгоду или расширить диапазон цен.' : 'Показываем только свежие ордера с положительной выгодой после комиссии. Сбор цен продолжается.'}</p>{hasFilters && <button className="button secondary" onClick={clearFilters}>Сбросить фильтры</button>}<div className="scan-indicator"><span className={`status-dot ${data?.sources.steam.state ?? 'loading'}`} />{data?.checked_total ? `Проверено ${number.format(data.checked_total)} из ${number.format(data.catalog_total)} предметов` : 'Подключение к источникам'}</div></div>}
          <div className="table-footer"><span>{filtered.length ? `${(actualPage - 1) * 15 + 1}–${Math.min(actualPage * 15, filtered.length)} из ${filtered.length} позиций` : '0 позиций'}<span className="footer-separator">·</span>Котировки на {date(data?.last_cycle_at)}</span><div className="pagination"><button className="icon-button" aria-label="Предыдущая страница" disabled={actualPage <= 1} onClick={() => setPage(actualPage - 1)}><ChevronLeft size={15} /></button><span>{actualPage}</span><button className="icon-button" aria-label="Следующая страница" disabled={actualPage >= pages} onClick={() => setPage(actualPage + 1)}><ChevronRight size={15} /></button></div></div>
        </section>

        <div className="below-table"><span><ShieldCheck size={15} />Комиссия Steam учтена в каждой позиции</span><button onClick={() => setModal('help')}>Как мы считаем<ArrowUpRight size={13} /></button></div>
        <section className="method-note"><div className="note-icon"><CircleHelp size={17} /></div><div><strong>Выгода на баланс Steam</strong><p>Выручка зачисляется в кошелёк Steam. Ордер может измениться до продажи. Обновляем экран каждые 10 секунд; предметы проверяются постепенно.</p></div><span className="note-label">БЕЗ АВТОПОКУПОК</span></section>
        <footer className="page-footer"><span>Независимый сервис сравнения цен</span><span>{fx ? `1 USD = ${fx.toFixed(4)} ₽ · ЦБ РФ, ${data?.fx?.date}` : 'Получаем курс валют'}<span className="footer-separator">·</span>₽ и $ — пересчёт исходных котировок</span></footer>
      </div>
    </main>

    {(modal || selected) && <div className="modal-backdrop" onClick={() => { setModal(null); setSelected(null) }}><section className="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title" onClick={event => event.stopPropagation()}><button className="modal-close icon-button" aria-label="Закрыть окно" onClick={() => { setModal(null); setSelected(null) }}><X size={20} /></button>
      {modal === 'settings' ? <><span className="modal-symbol"><SlidersHorizontal size={23} /></span><h2 id="modal-title">Настройки расчёта</h2><p>Учитывай полную стоимость покупки, чтобы видеть свою выгоду.</p><label className="settings-label">Комиссия пополнения Lis-Skins<div className="settings-input"><input autoFocus type="number" min="0" max="100" step="0.1" value={draftFee} onChange={event => setDraftFee(event.target.value)} /><span>%</span></div><small>Прибавляется к цене скина. Укажи комиссию своего способа оплаты.</small></label>{feeError && <p className="form-error" role="alert">{feeError}</p>}<div className="settings-info"><ShieldCheck size={17} /><span>Комиссия Steam: 5% площадке + 10% Dota 2. Минимумы и округления учтены.</span></div><div className="settings-info"><ArrowDownUp size={17} /><span>Котировки Steam в {Array.from(new Set(data?.items.map(item => item.source_currency) ?? [])).join(', ') || 'валюте ответа Steam'} приводятся к USD по курсу ЦБ. Рубли — пересчёт этих долларовых цен, а не отдельный рублёвый стакан.</span></div><button className="button primary full-width" onClick={saveSettings}>Применить настройки<Check size={16} /></button></>
      : modal === 'help' ? <><span className="modal-symbol"><TrendingUp size={23} /></span><h2 id="modal-title">Откуда берётся выгода</h2><p>Сравниваем покупку на Lis-Skins с продажей по самому высокому действующему ордеру Steam.</p><div className="formula-card"><span>Выгода, %</span><strong>(получишь − потратишь) / потратишь × 100</strong></div><ol className="help-steps"><li><strong>Потратишь</strong><span>Минимальная цена доступного скина на Lis-Skins плюс указанная тобой комиссия пополнения.</span></li><li><strong>Получишь</strong><span>Цена лучшего ордера Steam после двух комиссий. Это средства в кошельке Steam.</span></li><li><strong>Увидишь в таблице</strong><span>Только положительную разницу со свежими ценами и доступным объёмом лучшего ордера.</span></li></ol><div className="help-footnote">Полный каталог большой: проверяем предметы очередью, повторно проверяем найденные сделки и скрываем ордера старше {data?.quote_ttl_seconds ?? 120} секунд. Для предметов с дополнительными камнями или стилями требуется отдельное сопоставление; они исключены. Возможность продажи зависит от ограничений предмета и аккаунта.</div><a className="text-link" href="https://help.steampowered.com/en/faqs/view/61F0-72B7-9A18-C70B" target="_blank" rel="noopener noreferrer">Правила торговой площадки Steam<ExternalLink size={13} /></a></>
      : selected ? <><div className="detail-heading"><span className="detail-image">{selected.image && <img src={selected.image} alt="" />}</span><div><span className="eyebrow">DOTA 2</span><h2 id="modal-title">{selected.name}</h2></div></div>{!isFresh(selected) && <div className="notice error-notice">Котировка устарела. Дождись новой проверки перед покупкой.</div>}<div className="detail-grid"><div><span>Стоимость покупки</span><strong>{money(selected.cost_usd)}</strong></div><div><span>Ордер Steam</span><strong>{money(selected.steam_bid_usd)}</strong></div><div><span>На баланс Steam</span><strong>{money(selected.steam_net_usd)}</strong></div><div><span>Выгода</span><strong className="accent">+{money(selected.profit_usd)} / {selected.roi.toFixed(2)}%</strong></div></div><p className="detail-meta">Исходный ордер: {selected.source_bid.toFixed(2)} {selected.source_currency} · {selected.buy_order_quantity} шт.<br />Котировка на {date(selected.updated_at)} · На Lis-Skins: {selected.stock} шт.</p><div className="detail-actions"><a className="button primary" href={selected.lis_url} target="_blank" rel="noopener noreferrer">Найти на Lis-Skins<ArrowUpRight size={16} /></a><a className="button secondary" href={selected.steam_url} target="_blank" rel="noopener noreferrer">Проверить Steam<ExternalLink size={15} /></a></div></> : null}
    </section></div>}
  </div>
}
