export type SourceStatus = { state: 'ok' | 'loading' | 'error' | 'stale'; message: string }
export type Opportunity = {
  id: number; name: string; image: string | null; stock: number;
  lis_price_usd: number; cost_usd: number; steam_bid_usd: number; steam_net_usd: number;
  profit_usd: number; roi: number; source_currency: string; source_bid: number;
  buy_order_quantity: number; total_orders: number; updated_at: number; checked_at: number;
  lis_url: string; steam_url: string;
}
export type Snapshot = {
  items: Opportunity[]; server_time: number; interval_seconds: number; quote_ttl_seconds: number;
  catalog_total: number; checked_total: number; fresh_quotes: number; stale_quotes: number;
  profitable_total: number; best_roi: number | null; catalog_updated_at: number | null;
  catalog_checked_at: number | null; last_cycle_at: number | null; collecting: boolean;
  sources: Record<'lis' | 'steam' | 'fx', SourceStatus>;
  fx: { rub_per_usd: string; date: string; checked_at: number; usd_per_currency: Record<string, string> } | null;
  skipped_variants: number; deposit_fee_percent: number;
}
