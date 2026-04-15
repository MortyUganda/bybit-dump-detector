# Анализ: включены ли fee_pct, slippage_pct, funding_pct в итоговый pnl_pct

## Ответ: ДА, все три компонента включены

Файл: `app/services/auto_short_service.py`, строки 1564-1578.

### Как рассчитывается итоговый PnL при закрытии сделки:

1. **Slippage** — включён через корректировку exit_price (строки 1555-1562):
   - При SL: случайный slippage 0.01-0.1% добавляется к exit_price
   - При TP: случайный slippage 0.01-0.05% добавляется к exit_price
   - Slippage уже "запечён" в raw_pnl через скорректированную цену выхода

2. **Raw PnL** пересчитывается с учётом slippage-adjusted exit_price (строка 1566):
   ```python
   raw_pnl = await self._calc_short_pnl_pct(entry_price, exit_price)
   ```

3. **Fee** — рассчитывается как (entry_fee + exit_fee) × leverage × 100 (строка 1570):
   ```python
   fee_pct = (settings.paper_entry_fee + settings.paper_exit_fee) * leverage * 100
   ```

4. **Итоговая формула** (строка 1573):
   ```python
   final_pnl = raw_pnl - fee_pct + accumulated_funding_pct
   ```

### Вывод:
- `fee_pct` — **вычитается** из PnL (комиссии уменьшают прибыль)
- `slippage_pct` — **включён** через корректировку цены выхода (уже в raw_pnl)
- `funding_pct` — **прибавляется** (может быть + или -, зависит от ставки)

Все три компонента учтены в `final_pnl`, который сохраняется как `pnl_pct` в БД.
