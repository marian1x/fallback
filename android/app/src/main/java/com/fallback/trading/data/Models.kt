package com.fallback.trading.data

import com.squareup.moshi.Json

/**
 * Data-transfer objects mirroring the Flask JSON API (dashboard.py) plus the
 * small result wrapper the repository returns to the UI layer.
 */

/** GET /api/account */
data class AccountDto(
    val equity: Double = 0.0,
    val cash: Double = 0.0,
)

/** GET /api/open_positions (element) */
data class PositionDto(
    @Json(name = "user_id") val userId: Long? = null,
    val username: String? = null,
    val symbol: String = "",
    val side: String = "buy",
    val qty: Double = 0.0,
    @Json(name = "open_price") val openPrice: Double = 0.0,
    @Json(name = "current_price") val currentPrice: Double = 0.0,
    @Json(name = "market_value") val marketValue: Double = 0.0,
    @Json(name = "unrealized_pl") val unrealizedPl: Double = 0.0,
    @Json(name = "open_time_iso") val openTimeIso: String? = null,
)

/** GET /api/closed_orders (element) */
data class ClosedTradeDto(
    val symbol: String = "",
    val side: String = "",
    @Json(name = "open_price") val openPrice: Double? = null,
    @Json(name = "close_price") val closePrice: Double? = null,
    @Json(name = "profit_loss") val profitLoss: Double? = null,
    @Json(name = "profit_loss_pct") val profitLossPct: Double? = null,
    @Json(name = "open_time") val openTime: String? = null,
    @Json(name = "close_time") val closeTime: String? = null,
    val action: String? = null,
    val strategy: String? = null,
    @Json(name = "strategy_label") val strategyLabel: String? = null,
    @Json(name = "strategy_job_id") val strategyJobId: String? = null,
)

/** GET /api/tradable_symbols (element) */
data class SymbolDto(
    val symbol: String = "",
    val name: String = "",
    val exchange: String = "",
)

/** POST /api/proxy_trade body */
data class TradeRequest(
    val symbol: String,
    val action: String,                       // buy | sell | close
    val user: String = "Dashboard",
    val amount: Double? = null,
    @Json(name = "order_type") val orderType: String = "market",
    @Json(name = "time_in_force") val timeInForce: String = "day",
    @Json(name = "extended_hours") val extendedHours: Boolean = false,
    @Json(name = "limit_price") val limitPrice: Double? = null,
    // Admin order routing (ignored by the server for non-admin users).
    @Json(name = "dashboard_scope") val dashboardScope: String = "single",
    @Json(name = "dashboard_target_user_id") val dashboardTargetUserId: Long? = null,
)

/** GET /api/admin/dashboard_summary (element) */
data class AdminUserSummaryDto(
    val username: String = "",
    val equity: String = "",
    @Json(name = "open_pl") val openPl: String = "",
    @Json(name = "open_trades_count") val openTradesCount: Int = 0,
)

/** GET /api/admin/performance_leaderboard (element) */
data class LeaderboardEntryDto(
    val username: String = "",
    @Json(name = "total_pl") val totalPl: Double = 0.0,
    val wins: Int = 0,
    val losses: Int = 0,
    @Json(name = "total_trades") val totalTrades: Int = 0,
    @Json(name = "win_rate") val winRate: Double = 0.0,
)

/** POST /api/proxy_trade response (parsed leniently — the bot echoes varied shapes). */
data class TradeResponseDto(
    val result: String? = null,
    val code: String? = null,
    val message: String? = null,
    val status: String? = null,
    val error: String? = null,
    val detail: String? = null,
)

/** POST /api/stock_intelligence/ask response */
data class IntelAnswerDto(
    val answer: String = "",
    val symbols: List<String> = emptyList(),
    val model: String? = null,
    @Json(name = "latency_sec") val latencySec: Double? = null,
)

/** POST /api/stock_intelligence/analysis response */
data class AnalysisResponseDto(
    val symbols: List<String> = emptyList(),
    val results: Map<String, SymbolAnalysisDto> = emptyMap(),
)

data class SymbolAnalysisDto(
    @Json(name = "has_analysis") val hasAnalysis: Boolean = false,
    @Json(name = "narrative_summary") val narrativeSummary: String? = null,
    @Json(name = "analyst_stance") val analystStance: String? = null,
    @Json(name = "recurring_themes") val recurringThemes: List<String> = emptyList(),
    @Json(name = "dossier_updated_at") val dossierUpdatedAt: String? = null,
)

/** GitHub Releases API: GET /repos/{owner}/{repo}/releases/latest */
data class ReleaseDto(
    @Json(name = "tag_name") val tagName: String = "",
    val name: String? = null,
    val body: String? = null,
    @Json(name = "html_url") val htmlUrl: String? = null,
    val prerelease: Boolean = false,
    val assets: List<ReleaseAssetDto> = emptyList(),
)

data class ReleaseAssetDto(
    val name: String = "",
    @Json(name = "browser_download_url") val browserDownloadUrl: String = "",
    val size: Long = 0,
)

/** Parsed JSON error envelope returned by the API on failures. */
data class ApiErrorDto(
    val error: String? = null,
    val detail: String? = null,
    val message: String? = null,
)

/** Uniform result type handed to the UI. */
sealed interface ApiResult<out T> {
    data class Success<T>(val data: T) : ApiResult<T>
    data class Error(val message: String, val code: Int? = null) : ApiResult<Nothing>
    data object Unauthorized : ApiResult<Nothing>
}
