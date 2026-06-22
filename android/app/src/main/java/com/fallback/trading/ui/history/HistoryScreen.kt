package com.fallback.trading.ui.history

import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.ArrowDownward
import androidx.compose.material.icons.outlined.ArrowUpward
import androidx.compose.material.icons.outlined.Clear
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.DatePickerDialog
import androidx.compose.material3.DateRangePicker
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
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
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.fallback.trading.AppContainer
import com.fallback.trading.data.ClosedTradeDto
import com.fallback.trading.data.TradingRepository
import com.fallback.trading.ui.Format
import com.fallback.trading.ui.UiState
import com.fallback.trading.ui.components.EmptyState
import com.fallback.trading.ui.components.ErrorState
import com.fallback.trading.ui.components.LoadingState
import com.fallback.trading.ui.reduce
import com.fallback.trading.ui.theme.LossRed
import com.fallback.trading.ui.theme.ProfitGreen
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.drop
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import java.time.Instant
import java.time.LocalDate
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.ZoneOffset
import java.time.LocalDateTime
import java.time.format.DateTimeFormatter
import java.time.temporal.ChronoUnit

enum class HistoryPeriod(val label: String) { ALL("All"), TODAY("Today"), WEEK("Week"), MONTH("Month"), CUSTOM("Custom") }
enum class HistorySortBy { DATE, PL }

data class HistoryFilter(
    val symbolQuery: String = "",
    val period: HistoryPeriod = HistoryPeriod.ALL,
    val customStart: Long? = null,
    val customEnd: Long? = null,
    val sortBy: HistorySortBy = HistorySortBy.DATE,
    val ascending: Boolean = false,
)

class HistoryViewModel(private val repo: TradingRepository) : ViewModel() {
    private val _state = MutableStateFlow(UiState<List<ClosedTradeDto>>(loading = true))
    val state = _state.asStateFlow()

    private val _filter = MutableStateFlow(HistoryFilter())
    val filter = _filter.asStateFlow()

    val displayedTrades = combine(_state, _filter) { state, filter ->
        applyFilter(state.data ?: emptyList(), filter)
    }.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), emptyList())

    init {
        refresh()
        viewModelScope.launch {
            repo.adminState.scope.drop(1).collect { refresh() }
        }
    }

    fun refresh() {
        viewModelScope.launch {
            _state.update { it.copy(loading = true, error = null) }
            _state.update { it.reduce(repo.getClosedTrades()) }
        }
    }

    fun setFilter(filter: HistoryFilter) { _filter.value = filter }

    private fun applyFilter(trades: List<ClosedTradeDto>, filter: HistoryFilter): List<ClosedTradeDto> {
        val zone = ZoneId.systemDefault()
        val now = Instant.now()
        val (from: Instant?, to: Instant?) = when (filter.period) {
            HistoryPeriod.ALL -> null to null
            HistoryPeriod.TODAY -> {
                val start = LocalDate.now(zone).atStartOfDay(zone).toInstant()
                start to null
            }
            HistoryPeriod.WEEK -> now.minus(7, ChronoUnit.DAYS) to null
            HistoryPeriod.MONTH -> now.minus(30, ChronoUnit.DAYS) to null
            HistoryPeriod.CUSTOM -> {
                val start = filter.customStart?.let {
                    Instant.ofEpochMilli(it).atZone(zone).toLocalDate().atStartOfDay(zone).toInstant()
                }
                val end = filter.customEnd?.let {
                    Instant.ofEpochMilli(it).atZone(zone).toLocalDate().plusDays(1).atStartOfDay(zone).toInstant()
                }
                start to end
            }
        }
        return trades
            .filter { trade ->
                val symbolOk = filter.symbolQuery.isBlank() ||
                    trade.symbol.contains(filter.symbolQuery, ignoreCase = true)
                val t = parseInstant(trade.closeTime)
                val periodOk = when {
                    from == null && to == null -> true
                    t == null -> false
                    from != null && to != null -> t >= from && t < to
                    from != null -> t >= from
                    else -> t < to!!
                }
                symbolOk && periodOk
            }
            .sortedWith(Comparator { a, b ->
                val cmp = when (filter.sortBy) {
                    HistorySortBy.DATE -> compareValues(parseInstant(a.closeTime), parseInstant(b.closeTime))
                    HistorySortBy.PL -> compareValues(a.profitLoss, b.profitLoss)
                }
                if (filter.ascending) cmp else -cmp
            })
    }

    private fun parseInstant(iso: String?): Instant? {
        if (iso.isNullOrBlank()) return null
        return try { Instant.parse(iso) }
        catch (e: Exception) { try { OffsetDateTime.parse(iso).toInstant() }
        catch (e2: Exception) { try { LocalDateTime.parse(iso).toInstant(ZoneOffset.UTC) }
        catch (e3: Exception) { null } } }
    }

    companion object {
        fun factory(container: AppContainer) = viewModelFactory {
            initializer { HistoryViewModel(container.repository) }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun HistoryScreen(
    container: AppContainer,
    onSessionExpired: () -> Unit,
    viewModel: HistoryViewModel = viewModel(factory = HistoryViewModel.factory(container)),
) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    val filter by viewModel.filter.collectAsStateWithLifecycle()
    val displayed by viewModel.displayedTrades.collectAsStateWithLifecycle()

    LaunchedEffect(state.sessionExpired) {
        if (state.sessionExpired) onSessionExpired()
    }

    PullToRefreshBox(
        isRefreshing = state.loading,
        onRefresh = viewModel::refresh,
        modifier = Modifier.fillMaxSize(),
    ) {
        Column(Modifier.fillMaxSize()) {
            FilterBar(filter, onFilterChange = viewModel::setFilter)

            val trades = state.data
            when {
                trades != null -> {
                    if (displayed.isNotEmpty()) {
                        LazyColumn(
                            modifier = Modifier.fillMaxSize(),
                            contentPadding = PaddingValues(start = 16.dp, end = 16.dp, top = 8.dp, bottom = 96.dp),
                            verticalArrangement = Arrangement.spacedBy(12.dp),
                        ) {
                            itemsIndexed(displayed) { _, trade ->
                                ClosedTradeCard(trade)
                            }
                        }
                    } else {
                        EmptyState(
                            if (trades.isEmpty()) "No closed trades yet."
                            else "No trades match your filters."
                        )
                    }
                }
                state.loading -> LoadingState()
                state.error != null -> ErrorState(state.error!!, onRetry = viewModel::refresh)
            }
        }
    }
}

private val shortDateFmt: DateTimeFormatter = DateTimeFormatter.ofPattern("MMM d")

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun FilterBar(filter: HistoryFilter, onFilterChange: (HistoryFilter) -> Unit) {
    var showDatePicker by remember { mutableStateOf(false) }

    Column(modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp).padding(top = 8.dp)) {
        OutlinedTextField(
            value = filter.symbolQuery,
            onValueChange = { onFilterChange(filter.copy(symbolQuery = it)) },
            label = { Text("Symbol") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true,
            trailingIcon = {
                if (filter.symbolQuery.isNotBlank()) {
                    IconButton(onClick = { onFilterChange(filter.copy(symbolQuery = "")) }) {
                        Icon(Icons.Outlined.Clear, contentDescription = "Clear", modifier = Modifier.size(18.dp))
                    }
                }
            },
        )
        Row(
            modifier = Modifier.fillMaxWidth().padding(top = 8.dp, bottom = 4.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Row(
                modifier = Modifier
                    .weight(1f)
                    .horizontalScroll(rememberScrollState()),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                HistoryPeriod.entries.forEach { period ->
                    val label = if (period == HistoryPeriod.CUSTOM && filter.period == HistoryPeriod.CUSTOM &&
                        filter.customStart != null) {
                        buildCustomLabel(filter.customStart, filter.customEnd)
                    } else {
                        period.label
                    }
                    FilterChip(
                        selected = filter.period == period,
                        onClick = {
                            if (period == HistoryPeriod.CUSTOM) showDatePicker = true
                            else onFilterChange(filter.copy(period = period, customStart = null, customEnd = null))
                        },
                        label = { Text(label) },
                    )
                }
            }
            IconButton(onClick = { onFilterChange(filter.copy(ascending = !filter.ascending)) }) {
                Icon(
                    if (filter.ascending) Icons.Outlined.ArrowUpward else Icons.Outlined.ArrowDownward,
                    contentDescription = "Sort direction",
                    modifier = Modifier.size(20.dp),
                )
            }
            FilterChip(
                selected = filter.sortBy == HistorySortBy.PL,
                onClick = {
                    onFilterChange(
                        filter.copy(
                            sortBy = if (filter.sortBy == HistorySortBy.DATE) HistorySortBy.PL else HistorySortBy.DATE,
                        )
                    )
                },
                label = { Text(if (filter.sortBy == HistorySortBy.DATE) "Date" else "P&L") },
            )
        }
    }

    if (showDatePicker) {
        val pickerState = rememberDateRangePickerState(
            initialSelectedStartDateMillis = filter.customStart,
            initialSelectedEndDateMillis = filter.customEnd,
        )
        DatePickerDialog(
            onDismissRequest = { showDatePicker = false },
            confirmButton = {
                Button(onClick = {
                    onFilterChange(
                        filter.copy(
                            period = HistoryPeriod.CUSTOM,
                            customStart = pickerState.selectedStartDateMillis,
                            customEnd = pickerState.selectedEndDateMillis,
                        )
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
private fun ClosedTradeCard(trade: ClosedTradeDto) {
    val pl = trade.profitLoss ?: 0.0
    val plColor = if (pl >= 0) ProfitGreen else LossRed
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column {
                    Text(trade.symbol, style = MaterialTheme.typography.titleMedium)
                    Text(
                        Format.dateTime(trade.closeTime),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                Column(horizontalAlignment = Alignment.End) {
                    Text(
                        Format.moneySigned(pl),
                        style = MaterialTheme.typography.titleMedium,
                        color = plColor,
                        fontWeight = FontWeight.SemiBold,
                    )
                    Text(
                        Format.percentSigned(trade.profitLossPct),
                        style = MaterialTheme.typography.bodySmall,
                        color = plColor,
                    )
                }
            }
            Row(
                modifier = Modifier.fillMaxWidth().padding(top = 12.dp),
                horizontalArrangement = Arrangement.SpaceBetween,
            ) {
                Field("Side", trade.side.uppercase())
                Field("Open", Format.money(trade.openPrice))
                Field("Close", Format.money(trade.closePrice))
            }
            val strategy = trade.strategyLabel?.takeIf { it.isNotBlank() }
                ?: trade.strategy?.takeIf { it.isNotBlank() }
            if (strategy != null) {
                Text(
                    "Strategy: $strategy",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(top = 8.dp),
                )
            }
        }
    }
}

@Composable
private fun Field(label: String, value: String) {
    Column {
        Text(
            label.uppercase(),
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Text(value, style = MaterialTheme.typography.bodyLarge)
    }
}
