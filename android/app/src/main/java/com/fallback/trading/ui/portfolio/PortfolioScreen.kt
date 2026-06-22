package com.fallback.trading.ui.portfolio

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.AddChart
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
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
import com.fallback.trading.data.AdminUserSummaryDto
import com.fallback.trading.data.ApiResult
import com.fallback.trading.data.TradingRepository
import com.fallback.trading.ui.Format
import com.fallback.trading.ui.UiState
import com.fallback.trading.ui.components.AnimatedMoney
import com.fallback.trading.ui.components.ChangeBadge
import com.fallback.trading.ui.components.DonutChart
import com.fallback.trading.ui.components.DonutSlice
import com.fallback.trading.ui.components.ErrorState
import com.fallback.trading.ui.components.LegendDot
import com.fallback.trading.ui.components.LineChart
import com.fallback.trading.ui.components.LoadingState
import com.fallback.trading.ui.components.StatTile
import com.fallback.trading.ui.theme.BrandBlue
import com.fallback.trading.ui.theme.BrandGreen
import com.fallback.trading.ui.theme.BrandTeal
import com.fallback.trading.ui.theme.plColor
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.drop
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

private val AllocationColors = listOf(
    BrandGreen, BrandBlue, BrandTeal,
    Color(0xFFFFB020), Color(0xFFB37FEB), Color(0xFF8B97A7),
)

data class PortfolioData(
    val equity: Double,
    val cash: Double,
    val unrealized: Double,
    val realizedTotal: Double,
    val winRate: Double,
    val tradeCount: Int,
    val openCount: Int,
    val plSeries: List<Float>,
    val allocation: List<DonutSlice>,
    val scopeLabel: String = "",
    val isAdmin: Boolean = false,
    val adminSummary: List<AdminUserSummaryDto> = emptyList(),
)

class PortfolioViewModel(private val repo: TradingRepository) : ViewModel() {
    private val _state = MutableStateFlow(UiState<PortfolioData>(loading = true))
    val state = _state.asStateFlow()

    init {
        refresh()
        viewModelScope.launch {
            repo.adminState.scope.drop(1).collect { refresh() }
        }
        viewModelScope.launch {
            while (true) {
                delay(30_000)
                if (_state.value.data != null) refresh()
            }
        }
    }

    fun refresh() {
        viewModelScope.launch {
            _state.update { it.copy(loading = true, error = null) }
            when (val account = repo.getAccount()) {
                is ApiResult.Unauthorized -> _state.update { it.copy(loading = false, sessionExpired = true) }
                is ApiResult.Error -> _state.update { it.copy(loading = false, error = account.message) }
                is ApiResult.Success -> {
                    val (positions, closed) = coroutineScope {
                        val p = async { (repo.getOpenPositions() as? ApiResult.Success)?.data.orEmpty() }
                        val c = async { (repo.getClosedTrades() as? ApiResult.Success)?.data.orEmpty() }
                        p.await() to c.await()
                    }

                    val unrealized = positions.sumOf { it.unrealizedPl }
                    val sortedClosed = closed.sortedBy { it.closeTime ?: "" }
                    var running = 0f
                    val series = buildList {
                        add(0f)
                        sortedClosed.forEach { running += (it.profitLoss ?: 0.0).toFloat(); add(running) }
                    }
                    val realizedTotal = sortedClosed.sumOf { it.profitLoss ?: 0.0 }
                    val wins = closed.count { (it.profitLoss ?: 0.0) > 0 }
                    val winRate = if (closed.isNotEmpty()) wins * 100.0 / closed.size else 0.0

                    val bySymbol = positions
                        .groupBy { it.symbol }
                        .mapValues { (_, v) -> v.sumOf { kotlin.math.abs(it.marketValue) } }
                        .entries.sortedByDescending { it.value }
                    val top = bySymbol.take(5)
                    val otherTotal = bySymbol.drop(5).sumOf { it.value }
                    val allocation = buildList {
                        top.forEachIndexed { i, e ->
                            add(DonutSlice(e.key, e.value.toFloat(), AllocationColors[i % AllocationColors.size]))
                        }
                        if (otherTotal > 0) add(DonutSlice("Other", otherTotal.toFloat(), AllocationColors.last()))
                    }

                    val isAdmin = repo.adminState.isAdmin.value
                    val adminSummary = if (isAdmin) {
                        (repo.getAdminSummary() as? ApiResult.Success)?.data.orEmpty()
                    } else {
                        emptyList()
                    }

                    _state.update {
                        UiState(
                            data = PortfolioData(
                                equity = account.data.equity,
                                cash = account.data.cash,
                                unrealized = unrealized,
                                realizedTotal = realizedTotal,
                                winRate = winRate,
                                tradeCount = closed.size,
                                openCount = positions.size,
                                plSeries = series,
                                allocation = allocation,
                                scopeLabel = repo.adminState.scope.value.label,
                                isAdmin = isAdmin,
                                adminSummary = adminSummary,
                            )
                        )
                    }
                }
            }
        }
    }

    companion object {
        fun factory(container: AppContainer) = viewModelFactory {
            initializer { PortfolioViewModel(container.repository) }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PortfolioScreen(
    container: AppContainer,
    onTrade: () -> Unit,
    onSessionExpired: () -> Unit,
    viewModel: PortfolioViewModel = viewModel(factory = PortfolioViewModel.factory(container)),
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
        when {
            state.data != null -> PortfolioContent(state.data!!, onTrade)
            state.loading -> LoadingState()
            state.error != null -> ErrorState(state.error!!, onRetry = viewModel::refresh)
        }
    }
}

@Composable
private fun PortfolioContent(data: PortfolioData, onTrade: () -> Unit) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(start = 16.dp, end = 16.dp, top = 16.dp, bottom = 96.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        HeroCard(data)

        Row(horizontalArrangement = Arrangement.spacedBy(12.dp), modifier = Modifier.fillMaxWidth()) {
            StatTile("Unrealized P/L", Format.moneySigned(data.unrealized), Modifier.weight(1f), plColor(data.unrealized))
            StatTile("Realized P/L", Format.moneySigned(data.realizedTotal), Modifier.weight(1f), plColor(data.realizedTotal))
        }
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp), modifier = Modifier.fillMaxWidth()) {
            StatTile("Win rate", String.format(java.util.Locale.US, "%.0f%%", data.winRate), Modifier.weight(1f))
            StatTile("Open · closed", "${data.openCount} · ${data.tradeCount}", Modifier.weight(1f))
        }

        if (data.allocation.isNotEmpty()) {
            AllocationCard(data.allocation)
        }

        if (data.isAdmin && data.adminSummary.isNotEmpty()) {
            AdminSummaryCard(data.adminSummary)
        }

        Button(
            onClick = onTrade,
            modifier = Modifier.fillMaxWidth().height(52.dp),
        ) {
            Icon(Icons.Outlined.AddChart, contentDescription = null, modifier = Modifier.padding(end = 8.dp))
            Text("New trade", fontWeight = FontWeight.SemiBold)
        }
        Spacer(Modifier.height(4.dp))
    }
}

@Composable
private fun HeroCard(data: PortfolioData) {
    val glow = Brush.verticalGradient(
        listOf(BrandBlue.copy(alpha = 0.28f), Color.Transparent),
    )
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .clip(MaterialTheme.shapes.large)
            .background(MaterialTheme.colorScheme.surfaceContainerHigh)
            .background(glow)
            .padding(20.dp),
    ) {
        Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text(
                if (data.isAdmin) "PORTFOLIO VALUE · ${data.scopeLabel.uppercase()}" else "PORTFOLIO VALUE",
                style = MaterialTheme.typography.labelMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            AnimatedMoney(data.equity)
            Row(verticalAlignment = Alignment.CenterVertically) {
                ChangeBadge(data.unrealized)
                Spacer(Modifier.width(10.dp))
                Text(
                    "${Format.money(data.cash)} cash",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            LineChart(
                values = data.plSeries,
                lineColor = BrandGreen,
                modifier = Modifier
                    .fillMaxWidth()
                    .height(120.dp)
                    .padding(top = 8.dp),
            )
            Text(
                "Realized P/L · cumulative",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

@Composable
private fun AdminSummaryCard(summary: List<AdminUserSummaryDto>) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(20.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("Users", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
                Spacer(Modifier.width(8.dp))
                Text(
                    "${summary.size}",
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            summary.forEach { user ->
                Row(
                    modifier = Modifier.fillMaxWidth().padding(top = 14.dp),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Column(Modifier.weight(1f)) {
                        Text(user.username, style = MaterialTheme.typography.bodyLarge, fontWeight = FontWeight.SemiBold)
                        Text(
                            "${user.openTradesCount} open · P/L ${user.openPl}",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                    Text(user.equity, style = MaterialTheme.typography.titleMedium)
                }
            }
        }
    }
}

@Composable
private fun AllocationCard(slices: List<DonutSlice>) {
    val total = slices.sumOf { it.value.toDouble() }.takeIf { it > 0 } ?: 1.0
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(20.dp)) {
            Text("Allocation", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
            Row(
                modifier = Modifier.fillMaxWidth().padding(top = 16.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(20.dp),
            ) {
                DonutChart(slices = slices, modifier = Modifier.size(120.dp))
                Column(verticalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.weight(1f)) {
                    slices.forEach { slice ->
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            LegendDot(slice.color)
                            Spacer(Modifier.width(8.dp))
                            Text(
                                slice.label,
                                style = MaterialTheme.typography.bodyMedium,
                                modifier = Modifier.weight(1f),
                            )
                            Text(
                                String.format(java.util.Locale.US, "%.0f%%", slice.value / total * 100),
                                style = MaterialTheme.typography.bodyMedium,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                                fontWeight = FontWeight.SemiBold,
                            )
                        }
                    }
                }
            }
        }
    }
}
