package com.fallback.trading.ui.analytics

import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.DatePickerDialog
import androidx.compose.material3.DateRangePicker
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.material3.rememberDateRangePickerState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.fallback.trading.AppContainer
import com.fallback.trading.data.ApiResult
import com.fallback.trading.data.ClosedTradeDto
import com.fallback.trading.data.PositionDto
import com.fallback.trading.data.TradingRepository
import com.fallback.trading.ui.Format
import com.fallback.trading.ui.components.BarEntry
import com.fallback.trading.ui.components.DonutChart
import com.fallback.trading.ui.components.DonutSlice
import com.fallback.trading.ui.components.EmptyState
import com.fallback.trading.ui.components.ErrorState
import com.fallback.trading.ui.components.HorizontalBarChart
import com.fallback.trading.ui.components.LoadingState
import com.fallback.trading.ui.theme.LossRed
import com.fallback.trading.ui.theme.ProfitGreen
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.drop
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import java.time.Instant
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import java.time.temporal.ChronoUnit
import kotlin.math.abs

// ── Data model ─────────────────────────────────────────────────────────────────

enum class AnalyticsPeriod(val label: String) {
    ALL("All"), TODAY("Today"), WEEK("Week"), MONTH("Month"), YEAR("Year"), CUSTOM("Custom")
}

data class SymbolStats(
    val symbol: String,
    val closedCount: Int,
    val winRate: Double,
    val closedPl: Double,
    val openPl: Double,
)

data class AnalyticsData(
    val period: AnalyticsPeriod,
    val customStart: Long?,
    val customEnd: Long?,
    val positions: List<PositionDto>,
    val totalOpenPl: Double,
    val grossExposure: Double,
    val netExposure: Double,
    val longExposure: Double,
    val shortExposure: Double,
    val plBySymbol: List<BarEntry>,
    val exposureBySymbol: List<BarEntry>,
    val filteredClosed: List<ClosedTradeDto>,
    val totalClosedPl: Double,
    val winRate: Double,
    val bestPerformers: List<ClosedTradeDto>,
    val worstPerformers: List<ClosedTradeDto>,
    val symbolStats: List<SymbolStats>,
)

data class AnalyticsUiState(
    val loading: Boolean = true,
    val error: String? = null,
    val sessionExpired: Boolean = false,
    val data: AnalyticsData? = null,
    val period: AnalyticsPeriod = AnalyticsPeriod.ALL,
    val customStart: Long? = null,
    val customEnd: Long? = null,
)

// ── ViewModel ──────────────────────────────────────────────────────────────────

class AnalyticsViewModel(private val repo: TradingRepository) : ViewModel() {
    private val _state = MutableStateFlow(AnalyticsUiState())
    val state = _state.asStateFlow()

    @Volatile private var rawPositions: List<PositionDto> = emptyList()
    @Volatile private var rawClosed: List<ClosedTradeDto> = emptyList()
    private var initialized = false

    init {
        loadData()
        viewModelScope.launch {
            repo.adminState.scope.drop(1).collect {
                initialized = false
                loadData()
            }
        }
        viewModelScope.launch {
            while (true) {
                delay(15_000)
                if (initialized) silentRefresh()
            }
        }
    }

    fun loadData() {
        viewModelScope.launch {
            _state.update { it.copy(loading = true, error = null) }
            val ok = fetch()
            if (ok) {
                initialized = true
                recompute()
            }
        }
    }

    private suspend fun silentRefresh() {
        if (fetch()) recompute()
    }

    private suspend fun fetch(): Boolean = try {
        coroutineScope {
            val posDeferred = async { repo.getOpenPositions() }
            val closedDeferred = async { repo.getClosedTrades() }
            val posResult = posDeferred.await()
            val closedResult = closedDeferred.await()

            when (posResult) {
                is ApiResult.Unauthorized -> {
                    _state.update { it.copy(loading = false, sessionExpired = true) }
                    return@coroutineScope false
                }
                is ApiResult.Error -> {
                    _state.update { it.copy(loading = false, error = posResult.message) }
                    return@coroutineScope false
                }
                is ApiResult.Success -> rawPositions = posResult.data
            }
            if (closedResult is ApiResult.Success) rawClosed = closedResult.data
            true
        }
    } catch (e: Exception) {
        _state.update { it.copy(loading = false, error = e.message ?: "Load failed") }
        false
    }

    fun setPeriod(period: AnalyticsPeriod, customStart: Long? = null, customEnd: Long? = null) {
        _state.update { it.copy(period = period, customStart = customStart, customEnd = customEnd) }
        recompute()
    }

    private fun recompute() {
        val s = _state.value
        _state.update {
            it.copy(loading = false, data = compute(rawPositions, rawClosed, s.period, s.customStart, s.customEnd))
        }
    }

    private fun compute(
        positions: List<PositionDto>,
        closed: List<ClosedTradeDto>,
        period: AnalyticsPeriod,
        customStart: Long?,
        customEnd: Long?,
    ): AnalyticsData {
        val zone = ZoneId.systemDefault()
        val (from: Instant?, to: Instant?) = when (period) {
            AnalyticsPeriod.ALL -> null to null
            AnalyticsPeriod.TODAY -> {
                val start = LocalDate.now(zone).atStartOfDay(zone).toInstant()
                start to null
            }
            AnalyticsPeriod.WEEK -> Instant.now().minus(7, ChronoUnit.DAYS) to null
            AnalyticsPeriod.MONTH -> Instant.now().minus(30, ChronoUnit.DAYS) to null
            AnalyticsPeriod.YEAR -> Instant.now().minus(365, ChronoUnit.DAYS) to null
            AnalyticsPeriod.CUSTOM -> {
                val start = customStart?.let {
                    Instant.ofEpochMilli(it).atZone(zone).toLocalDate().atStartOfDay(zone).toInstant()
                }
                val end = customEnd?.let {
                    Instant.ofEpochMilli(it).atZone(zone).toLocalDate().plusDays(1).atStartOfDay(zone).toInstant()
                }
                start to end
            }
        }

        val filtered = closed.filter { trade ->
            val t = parseInstant(trade.closeTime)
            when {
                from == null && to == null -> true
                t == null -> false
                from != null && to != null -> t >= from && t < to
                from != null -> t >= from
                else -> t < to!!
            }
        }

        val totalOpenPl = positions.sumOf { it.unrealizedPl }
        val grossExposure = positions.sumOf { abs(it.marketValue) }
        val netExposure = positions.sumOf { it.marketValue }
        val longExposure = positions.sumOf { maxOf(it.marketValue, 0.0) }
        val shortExposure = positions.sumOf { -minOf(it.marketValue, 0.0) }

        val plBySymbol = positions
            .groupBy { it.symbol }
            .map { (sym, pos) -> BarEntry(sym, pos.sumOf { it.unrealizedPl }.toFloat()) }
            .sortedByDescending { abs(it.value) }.take(8)

        val exposureBySymbol = positions
            .groupBy { it.symbol }
            .map { (sym, pos) -> BarEntry(sym, pos.sumOf { abs(it.marketValue) }.toFloat()) }
            .sortedByDescending { it.value }.take(8)

        val totalClosedPl = filtered.sumOf { it.profitLoss ?: 0.0 }
        val wins = filtered.count { (it.profitLoss ?: 0.0) > 0 }
        val winRate = if (filtered.isNotEmpty()) wins * 100.0 / filtered.size else 0.0

        val best = filtered.filter { (it.profitLoss ?: 0.0) > 0 }
            .sortedByDescending { it.profitLoss ?: 0.0 }.take(5)
        val worst = filtered.filter { (it.profitLoss ?: 0.0) < 0 }
            .sortedBy { it.profitLoss ?: 0.0 }.take(5)

        val openPlMap = positions.groupBy { it.symbol }.mapValues { (_, v) -> v.sumOf { it.unrealizedPl } }
        val allSymbols = (filtered.map { it.symbol } + positions.map { it.symbol }).toSet()
        val symbolStats = allSymbols.map { sym ->
            val trades = filtered.filter { it.symbol == sym }
            val tradeWins = trades.count { (it.profitLoss ?: 0.0) > 0 }
            SymbolStats(
                symbol = sym,
                closedCount = trades.size,
                winRate = if (trades.isNotEmpty()) tradeWins * 100.0 / trades.size else 0.0,
                closedPl = trades.sumOf { it.profitLoss ?: 0.0 },
                openPl = openPlMap[sym] ?: 0.0,
            )
        }.sortedByDescending { abs(it.closedPl + it.openPl) }

        return AnalyticsData(
            period = period,
            customStart = customStart,
            customEnd = customEnd,
            positions = positions,
            totalOpenPl = totalOpenPl,
            grossExposure = grossExposure,
            netExposure = netExposure,
            longExposure = longExposure,
            shortExposure = shortExposure,
            plBySymbol = plBySymbol,
            exposureBySymbol = exposureBySymbol,
            filteredClosed = filtered,
            totalClosedPl = totalClosedPl,
            winRate = winRate,
            bestPerformers = best,
            worstPerformers = worst,
            symbolStats = symbolStats,
        )
    }

    private fun parseInstant(iso: String?): Instant? {
        if (iso.isNullOrBlank()) return null
        return try { Instant.parse(iso) } catch (e: Exception) {
            try { OffsetDateTime.parse(iso).toInstant() } catch (e2: Exception) {
                try { LocalDateTime.parse(iso).toInstant(ZoneOffset.UTC) } catch (e3: Exception) { null }
            }
        }
    }

    companion object {
        fun factory(container: AppContainer) = viewModelFactory {
            initializer { AnalyticsViewModel(container.repository) }
        }
    }
}

// ── Screen ─────────────────────────────────────────────────────────────────────

private val shortDateFmt: DateTimeFormatter = DateTimeFormatter.ofPattern("MMM d")

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AnalyticsScreen(
    container: AppContainer,
    onSessionExpired: () -> Unit,
    viewModel: AnalyticsViewModel = viewModel(factory = AnalyticsViewModel.factory(container)),
) {
    val uiState by viewModel.state.collectAsStateWithLifecycle()
    var showDatePicker by remember { mutableStateOf(false) }

    LaunchedEffect(uiState.sessionExpired) {
        if (uiState.sessionExpired) onSessionExpired()
    }

    if (showDatePicker) {
        val pickerState = rememberDateRangePickerState(
            initialSelectedStartDateMillis = uiState.customStart,
            initialSelectedEndDateMillis = uiState.customEnd,
        )
        DatePickerDialog(
            onDismissRequest = { showDatePicker = false },
            confirmButton = {
                Button(onClick = {
                    viewModel.setPeriod(
                        AnalyticsPeriod.CUSTOM,
                        pickerState.selectedStartDateMillis,
                        pickerState.selectedEndDateMillis,
                    )
                    showDatePicker = false
                }) { Text("Apply") }
            },
            dismissButton = {
                TextButton(onClick = { showDatePicker = false }) { Text("Cancel") }
            },
        ) {
            DateRangePicker(state = pickerState, modifier = Modifier.weight(1f))
        }
    }

    PullToRefreshBox(
        isRefreshing = uiState.loading,
        onRefresh = viewModel::loadData,
        modifier = Modifier.fillMaxSize(),
    ) {
        Column(Modifier.fillMaxSize()) {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .horizontalScroll(rememberScrollState())
                    .padding(horizontal = 16.dp, vertical = 8.dp),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                AnalyticsPeriod.entries.forEach { period ->
                    val label = if (period == AnalyticsPeriod.CUSTOM &&
                        uiState.period == AnalyticsPeriod.CUSTOM &&
                        uiState.customStart != null) {
                        buildCustomLabel(uiState.customStart, uiState.customEnd)
                    } else {
                        period.label
                    }
                    FilterChip(
                        selected = uiState.period == period,
                        onClick = {
                            if (period == AnalyticsPeriod.CUSTOM) showDatePicker = true
                            else viewModel.setPeriod(period)
                        },
                        label = { Text(label) },
                    )
                }
            }

            val data = uiState.data
            when {
                data != null -> AnalyticsContent(data)
                uiState.loading -> LoadingState()
                uiState.error != null -> ErrorState(uiState.error!!, onRetry = viewModel::loadData)
                else -> EmptyState("No data available.")
            }
        }
    }
}

private fun buildCustomLabel(startMillis: Long?, endMillis: Long?): String {
    val zone = ZoneId.systemDefault()
    val start = startMillis?.let { Instant.ofEpochMilli(it).atZone(zone).toLocalDate() }
    val end = endMillis?.let { Instant.ofEpochMilli(it).atZone(zone).toLocalDate() }
    return when {
        start != null && end != null && start != end ->
            "${shortDateFmt.format(start)} – ${shortDateFmt.format(end)}"
        start != null -> shortDateFmt.format(start)
        else -> "Custom"
    }
}

@Composable
private fun AnalyticsContent(data: AnalyticsData) {
    val periodLabel = if (data.period == AnalyticsPeriod.CUSTOM) {
        buildCustomLabel(data.customStart, data.customEnd)
    } else {
        data.period.label
    }

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(start = 16.dp, end = 16.dp, bottom = 96.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        // ── Open Positions ─────────────────────────────────────────────
        item {
            SectionHeader("Open Positions", "${data.positions.size} active")
        }
        item {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                MetricCard("Unrealized P/L", Format.moneySigned(data.totalOpenPl),
                    if (data.totalOpenPl >= 0) ProfitGreen else LossRed, Modifier.weight(1f))
                MetricCard("Gross Exposure", Format.money(data.grossExposure),
                    MaterialTheme.colorScheme.onSurface, Modifier.weight(1f))
            }
        }
        item {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                MetricCard("Net Exposure", Format.moneySigned(data.netExposure),
                    if (data.netExposure >= 0) ProfitGreen else LossRed, Modifier.weight(1f))
                Card(Modifier.weight(1f)) {
                    Column(Modifier.padding(14.dp)) {
                        Text("Long / Short", style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                        Spacer(Modifier.height(4.dp))
                        Row(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = Alignment.CenterVertically) {
                            DonutChart(
                                slices = listOf(
                                    DonutSlice("L", data.longExposure.toFloat(), ProfitGreen),
                                    DonutSlice("S", data.shortExposure.toFloat(), LossRed),
                                ),
                                modifier = Modifier.size(36.dp),
                                strokeWidth = 8.dp,
                            )
                            Column {
                                Text(Format.money(data.longExposure), style = MaterialTheme.typography.labelSmall, color = ProfitGreen)
                                Text(Format.money(data.shortExposure), style = MaterialTheme.typography.labelSmall, color = LossRed)
                            }
                        }
                    }
                }
            }
        }

        if (data.plBySymbol.isNotEmpty()) {
            item {
                Card(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(16.dp)) {
                        Text("Open P/L by Symbol", style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold)
                        Spacer(Modifier.height(12.dp))
                        HorizontalBarChart(data.plBySymbol, Modifier.fillMaxWidth(), positiveColor = ProfitGreen, negativeColor = LossRed)
                    }
                }
            }
        }

        if (data.exposureBySymbol.isNotEmpty()) {
            item {
                Card(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(16.dp)) {
                        Text("Exposure by Symbol", style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold)
                        Spacer(Modifier.height(12.dp))
                        HorizontalBarChart(data.exposureBySymbol, Modifier.fillMaxWidth())
                    }
                }
            }
        }

        // ── Closed Trades ──────────────────────────────────────────────
        item {
            Spacer(Modifier.height(4.dp))
            SectionHeader("Closed Trades", "${data.filteredClosed.size} · $periodLabel")
        }

        if (data.filteredClosed.isEmpty()) {
            item {
                Text(
                    "No closed trades in this period.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(bottom = 8.dp),
                )
            }
        } else {
            item {
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    MetricCard("Total P/L", Format.moneySigned(data.totalClosedPl),
                        if (data.totalClosedPl >= 0) ProfitGreen else LossRed, Modifier.weight(1f))
                    MetricCard(
                        "Win Rate",
                        String.format(java.util.Locale.US, "%.0f%%", data.winRate),
                        MaterialTheme.colorScheme.onSurface,
                        Modifier.weight(1f),
                    )
                }
            }

            if (data.bestPerformers.isNotEmpty()) {
                item {
                    Card(Modifier.fillMaxWidth()) {
                        Column(Modifier.padding(16.dp)) {
                            Text("Best Performers", style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold, color = ProfitGreen)
                            Spacer(Modifier.height(8.dp))
                            data.bestPerformers.forEachIndexed { i, trade ->
                                if (i > 0) HorizontalDivider(modifier = Modifier.padding(vertical = 6.dp))
                                PerformerRow(trade)
                            }
                        }
                    }
                }
            }

            if (data.worstPerformers.isNotEmpty()) {
                item {
                    Card(Modifier.fillMaxWidth()) {
                        Column(Modifier.padding(16.dp)) {
                            Text("Worst Performers", style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold, color = LossRed)
                            Spacer(Modifier.height(8.dp))
                            data.worstPerformers.forEachIndexed { i, trade ->
                                if (i > 0) HorizontalDivider(modifier = Modifier.padding(vertical = 6.dp))
                                PerformerRow(trade)
                            }
                        }
                    }
                }
            }
        }

        // ── By Symbol ──────────────────────────────────────────────────
        if (data.symbolStats.isNotEmpty()) {
            item {
                Spacer(Modifier.height(4.dp))
                SectionHeader("By Symbol", "")
            }
            item {
                Card(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(16.dp)) {
                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                            Text("Symbol", style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.weight(1f))
                            Text("Trades", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                            Text("  Win%", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                            Text("   P/L", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                        HorizontalDivider(modifier = Modifier.padding(vertical = 6.dp))
                        data.symbolStats.forEachIndexed { i, stat ->
                            if (i > 0) HorizontalDivider(modifier = Modifier.padding(vertical = 4.dp), thickness = 0.5.dp)
                            SymbolStatRow(stat)
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun SectionHeader(title: String, subtitle: String) {
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(title, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
        if (subtitle.isNotBlank()) {
            Text(subtitle, style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
private fun MetricCard(label: String, value: String, valueColor: Color, modifier: Modifier = Modifier) {
    Card(modifier = modifier) {
        Column(Modifier.padding(14.dp)) {
            Text(label, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.height(4.dp))
            Text(value, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold, color = valueColor)
        }
    }
}

@Composable
private fun PerformerRow(trade: ClosedTradeDto) {
    val pl = trade.profitLoss ?: 0.0
    val color = if (pl >= 0) ProfitGreen else LossRed
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f)) {
            Text(trade.symbol, style = MaterialTheme.typography.bodyMedium, fontWeight = FontWeight.SemiBold,
                maxLines = 1, overflow = TextOverflow.Ellipsis)
            Text(Format.dateTime(trade.closeTime), style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Column(horizontalAlignment = Alignment.End) {
            Text(Format.moneySigned(pl), style = MaterialTheme.typography.bodyMedium, color = color, fontWeight = FontWeight.SemiBold)
            Text(Format.percentSigned(trade.profitLossPct), style = MaterialTheme.typography.labelSmall, color = color)
        }
    }
}

@Composable
private fun SymbolStatRow(stat: SymbolStats) {
    val totalPl = stat.closedPl + stat.openPl
    val color = if (totalPl >= 0) ProfitGreen else LossRed
    Row(
        Modifier.fillMaxWidth().padding(vertical = 2.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(stat.symbol, style = MaterialTheme.typography.bodyMedium, fontWeight = FontWeight.SemiBold,
            modifier = Modifier.weight(1f), maxLines = 1, overflow = TextOverflow.Ellipsis)
        Text("${stat.closedCount}", style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(start = 8.dp))
        Text(
            if (stat.closedCount > 0) String.format(java.util.Locale.US, "  %.0f%%", stat.winRate) else "   —",
            style = MaterialTheme.typography.bodySmall,
        )
        Text(Format.moneySigned(totalPl), style = MaterialTheme.typography.bodySmall, color = color,
            fontWeight = FontWeight.SemiBold, modifier = Modifier.padding(start = 8.dp))
    }
}
