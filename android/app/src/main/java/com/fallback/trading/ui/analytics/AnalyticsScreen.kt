package com.fallback.trading.ui.analytics

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
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.fallback.trading.AppContainer
import com.fallback.trading.data.ApiResult
import com.fallback.trading.data.PositionDto
import com.fallback.trading.data.TradingRepository
import com.fallback.trading.ui.Format
import com.fallback.trading.ui.UiState
import com.fallback.trading.ui.components.BarEntry
import com.fallback.trading.ui.components.DonutChart
import com.fallback.trading.ui.components.DonutSlice
import com.fallback.trading.ui.components.EmptyState
import com.fallback.trading.ui.components.ErrorState
import com.fallback.trading.ui.components.HorizontalBarChart
import com.fallback.trading.ui.components.LoadingState
import com.fallback.trading.ui.theme.LossRed
import com.fallback.trading.ui.theme.ProfitGreen
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.drop
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlin.math.abs

data class AnalyticsData(
    val positions: List<PositionDto>,
    val totalPl: Double,
    val grossExposure: Double,
    val netExposure: Double,
    val longExposure: Double,
    val shortExposure: Double,
    val topPosition: PositionDto?,
    val plBySymbol: List<BarEntry>,
    val exposureBySymbol: List<BarEntry>,
)

class AnalyticsViewModel(private val repo: TradingRepository) : ViewModel() {
    private val _state = MutableStateFlow(UiState<AnalyticsData>(loading = true))
    val state = _state.asStateFlow()

    init {
        refresh()
        viewModelScope.launch {
            repo.adminState.scope.drop(1).collect { refresh() }
        }
        viewModelScope.launch {
            while (true) {
                delay(15_000)
                refresh()
            }
        }
    }

    fun refresh() {
        viewModelScope.launch {
            _state.update { it.copy(loading = true, error = null) }
            when (val result = repo.getOpenPositions()) {
                is ApiResult.Success -> {
                    val positions = result.data
                    val totalPl = positions.sumOf { it.unrealizedPl }
                    val grossExposure = positions.sumOf { abs(it.marketValue) }
                    val netExposure = positions.sumOf { it.marketValue }
                    val longExposure = positions.sumOf { maxOf(it.marketValue, 0.0) }
                    val shortExposure = positions.sumOf { -minOf(it.marketValue, 0.0) }
                    val topPosition = positions.maxByOrNull { abs(it.unrealizedPl) }

                    val plBySymbol = positions
                        .groupBy { it.symbol }
                        .map { (sym, pos) -> BarEntry(sym, pos.sumOf { it.unrealizedPl }.toFloat()) }
                        .sortedByDescending { abs(it.value) }
                        .take(8)

                    val exposureBySymbol = positions
                        .groupBy { it.symbol }
                        .map { (sym, pos) -> BarEntry(sym, pos.sumOf { abs(it.marketValue) }.toFloat()) }
                        .sortedByDescending { it.value }
                        .take(8)

                    _state.update {
                        UiState(
                            data = AnalyticsData(
                                positions = positions,
                                totalPl = totalPl,
                                grossExposure = grossExposure,
                                netExposure = netExposure,
                                longExposure = longExposure,
                                shortExposure = shortExposure,
                                topPosition = topPosition,
                                plBySymbol = plBySymbol,
                                exposureBySymbol = exposureBySymbol,
                            )
                        )
                    }
                }
                is ApiResult.Unauthorized -> _state.update { it.copy(loading = false, sessionExpired = true) }
                is ApiResult.Error -> _state.update { it.copy(loading = false, error = result.message) }
            }
        }
    }

    companion object {
        fun factory(container: AppContainer) = viewModelFactory {
            initializer { AnalyticsViewModel(container.repository) }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AnalyticsScreen(
    container: AppContainer,
    onSessionExpired: () -> Unit,
    viewModel: AnalyticsViewModel = viewModel(factory = AnalyticsViewModel.factory(container)),
) {
    val state by viewModel.state.collectAsStateWithLifecycle()

    LaunchedEffect(state.sessionExpired) {
        if (state.sessionExpired) onSessionExpired()
    }

    PullToRefreshBox(
        isRefreshing = state.loading,
        onRefresh = viewModel::refresh,
        modifier = Modifier.fillMaxSize(),
    ) {
        val data = state.data
        when {
            data != null -> AnalyticsContent(data)
            state.loading -> LoadingState()
            state.error != null -> ErrorState(state.error!!, onRetry = viewModel::refresh)
            else -> EmptyState("No open positions to analyze.")
        }
    }
}

@Composable
private fun AnalyticsContent(data: AnalyticsData) {
    val plColor = if (data.totalPl >= 0) ProfitGreen else LossRed
    val netColor = if (data.netExposure >= 0) ProfitGreen else LossRed

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(start = 16.dp, end = 16.dp, top = 16.dp, bottom = 96.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        item {
            Text("Summary", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
        }
        item {
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                MetricCard("Total P/L", Format.moneySigned(data.totalPl), plColor, Modifier.weight(1f))
                MetricCard("Positions", "${data.positions.size}", MaterialTheme.colorScheme.onSurface, Modifier.weight(1f))
            }
        }
        item {
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                MetricCard("Gross Exposure", Format.money(data.grossExposure), MaterialTheme.colorScheme.onSurface, Modifier.weight(1f))
                MetricCard("Net Exposure", Format.moneySigned(data.netExposure), netColor, Modifier.weight(1f))
            }
        }

        if (data.topPosition != null) {
            item {
                val tp = data.topPosition
                val tpColor = if (tp.unrealizedPl >= 0) ProfitGreen else LossRed
                Card(modifier = Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(16.dp)) {
                        Text(
                            "Top Position",
                            style = MaterialTheme.typography.labelMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                        Spacer(Modifier.height(6.dp))
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.SpaceBetween,
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Column {
                                Text(tp.symbol, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
                                Text(
                                    tp.side.uppercase(),
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                            }
                            Text(
                                Format.moneySigned(tp.unrealizedPl),
                                style = MaterialTheme.typography.titleMedium,
                                color = tpColor,
                                fontWeight = FontWeight.Bold,
                            )
                        }
                    }
                }
            }
        }

        item {
            Card(modifier = Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp)) {
                    Text("Long vs Short", style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold)
                    Spacer(Modifier.height(12.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        DonutChart(
                            slices = listOf(
                                DonutSlice("Long", data.longExposure.toFloat(), ProfitGreen),
                                DonutSlice("Short", data.shortExposure.toFloat(), LossRed),
                            ),
                            modifier = Modifier.size(88.dp),
                        )
                        Column(
                            modifier = Modifier.padding(start = 20.dp),
                            verticalArrangement = Arrangement.spacedBy(6.dp),
                        ) {
                            LegendItem("Long", Format.money(data.longExposure), ProfitGreen)
                            LegendItem("Short", Format.money(data.shortExposure), LossRed)
                        }
                    }
                }
            }
        }

        if (data.plBySymbol.isNotEmpty()) {
            item {
                Card(modifier = Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(16.dp)) {
                        Text("P/L by Symbol", style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold)
                        Spacer(Modifier.height(12.dp))
                        HorizontalBarChart(
                            entries = data.plBySymbol,
                            modifier = Modifier.fillMaxWidth(),
                            positiveColor = ProfitGreen,
                            negativeColor = LossRed,
                        )
                    }
                }
            }
        }

        if (data.exposureBySymbol.isNotEmpty()) {
            item {
                Card(modifier = Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(16.dp)) {
                        Text("Exposure by Symbol", style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold)
                        Spacer(Modifier.height(12.dp))
                        HorizontalBarChart(
                            entries = data.exposureBySymbol,
                            modifier = Modifier.fillMaxWidth(),
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun MetricCard(label: String, value: String, valueColor: Color, modifier: Modifier = Modifier) {
    Card(modifier = modifier) {
        Column(Modifier.padding(14.dp)) {
            Text(
                label,
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.height(4.dp))
            Text(
                value,
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.Bold,
                color = valueColor,
            )
        }
    }
}

@Composable
private fun LegendItem(label: String, value: String, color: Color) {
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Surface(color = color, shape = MaterialTheme.shapes.extraSmall, modifier = Modifier.size(10.dp)) {}
        Column {
            Text(label, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            Text(value, style = MaterialTheme.typography.bodySmall, fontWeight = FontWeight.SemiBold)
        }
    }
}
