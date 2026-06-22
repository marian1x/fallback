package com.fallback.trading.data

import com.squareup.moshi.Moshi
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import retrofit2.Response

/**
 * Single source of truth for all backend interaction.
 *
 * Auth model (mirrors the Flask app): a session cookie plus a CSRF token that is
 * embedded in every HTML page as `<meta name="csrf-token">`. We log in with a
 * form POST, scrape the refreshed token from the dashboard, and attach it to all
 * later state-changing requests via [CsrfInterceptor].
 */
class TradingRepository(
    private val network: NetworkClient,
    private val session: SessionState,
    private val cookieJar: PersistentCookieJar,
    private val settings: SettingsStore,
    private val secureStore: SecureStore,
    val adminState: AdminState,
    moshi: Moshi,
) {
    private val errorAdapter = moshi.adapter(ApiErrorDto::class.java)

    @Volatile
    private var cachedSymbols: List<SymbolDto>? = null

    val baseUrl: String? get() = network.baseUrl

    fun isConfigured(): Boolean = network.baseUrl != null

    suspend fun configureFromSettings(): Boolean {
        val url = settings.baseUrlOnce() ?: return false
        network.setBaseUrl(url)
        return true
    }

    suspend fun setServer(rawUrl: String): ApiResult<Unit> = withContext(Dispatchers.IO) {
        val normalized = SettingsStore.normalizeBaseUrl(rawUrl)
            ?: return@withContext ApiResult.Error("Enter a valid server URL.")
        network.setBaseUrl(normalized)
        // Probe reachability with a lightweight request; a redirect to /login still
        // proves the host is up and speaking to our app.
        try {
            val resp = network.requireApi().getLoginPage()
            if (resp.isSuccessful || resp.code() in 300..399) {
                settings.setBaseUrl(normalized)
                ApiResult.Success(Unit)
            } else {
                ApiResult.Error("Server responded with HTTP ${resp.code()}.", resp.code())
            }
        } catch (e: Exception) {
            ApiResult.Error("Cannot reach server: ${e.friendly()}")
        }
    }

    // --- Authentication ---------------------------------------------------

    suspend fun login(username: String, password: String): ApiResult<Unit> =
        withContext(Dispatchers.IO) {
            val api = network.requireApi()
            try {
                val page = api.getLoginPage()
                if (!page.isSuccessful) {
                    return@withContext ApiResult.Error("Could not load login page (HTTP ${page.code()}).")
                }
                val csrf = extractCsrf(page.body()?.string())
                    ?: return@withContext ApiResult.Error("Could not read security token from server.")
                session.csrfToken = csrf

                val resp = api.postLogin(username, password, csrf)
                when {
                    resp.code() in 300..399 -> {
                        // 302 -> dashboard means the credentials were accepted.
                        session.csrfToken = null
                        refreshCsrf(parseAdmin = true)
                        settings.setLastUsername(username)
                        ApiResult.Success(Unit)
                    }
                    resp.code() == 429 ->
                        ApiResult.Error("Too many attempts. Try again in a few minutes.", 429)
                    resp.code() == 200 ->
                        ApiResult.Error("Invalid username or password.")
                    else ->
                        ApiResult.Error("Login failed (HTTP ${resp.code()}).", resp.code())
                }
            } catch (e: Exception) {
                ApiResult.Error("Login failed: ${e.friendly()}")
            }
        }

    /** Returns true if the persisted session cookie is still valid. */
    suspend fun resumeSession(): Boolean = withContext(Dispatchers.IO) {
        if (!isConfigured()) return@withContext false
        try {
            val resp = network.requireApi().getAccount()
            if (resp.isSuccessful) {
                refreshCsrf(parseAdmin = true)   // recover token + admin state after process death
                true
            } else {
                false
            }
        } catch (e: Exception) {
            false
        }
    }

    suspend fun logout() = withContext(Dispatchers.IO) {
        runCatching { network.requireApi().postLogout() }
        session.csrfToken = null
        cachedSymbols = null
        adminState.reset()
        cookieJar.clear()
    }

    private suspend fun refreshCsrf(parseAdmin: Boolean = false): Boolean {
        return try {
            val resp = network.requireApi().getRoot()
            if (resp.isSuccessful) {
                val html = resp.body()?.string()
                val token = extractCsrf(html)
                session.csrfToken = token
                if (parseAdmin) parseAdminInfo(html)
                token != null
            } else {
                false
            }
        } catch (e: Exception) {
            false
        }
    }

    private fun parseAdminInfo(html: String?) {
        if (html == null) return
        val isAdmin = html.contains("id=\"adminTradeScope\"") || html.contains("id=\"adminTargetUser\"")
        val users = if (isAdmin) {
            ADMIN_USER_OPTION.findAll(html)
                .mapNotNull { m ->
                    val id = m.groupValues[1].toLongOrNull() ?: return@mapNotNull null
                    AdminUser(id, m.groupValues[2].trim())
                }
                .distinctBy { it.id }
                .toList()
        } else {
            emptyList()
        }
        adminState.update(isAdmin, users)
    }

    // --- Read APIs --------------------------------------------------------

    suspend fun getAccount(): ApiResult<AccountDto> {
        val (scope, userId) = adminState.scope.value.accountParams()
        return ioCall { network.requireApi().getAccount(scope, userId) }
    }

    suspend fun getOpenPositions(): ApiResult<List<PositionDto>> {
        val (scope, userId) = adminState.scope.value.positionsParams()
        return ioCall { network.requireApi().getOpenPositions(scope, userId) }
    }

    suspend fun getClosedTrades(): ApiResult<List<ClosedTradeDto>> =
        ioCall { network.requireApi().getClosedOrders(adminState.scope.value.closedUserId()) }

    suspend fun getAdminSummary(): ApiResult<List<AdminUserSummaryDto>> =
        ioCall { network.requireApi().getAdminSummary() }

    suspend fun getLeaderboard(): ApiResult<List<LeaderboardEntryDto>> =
        ioCall { network.requireApi().getLeaderboard() }

    suspend fun getTradableSymbols(forceRefresh: Boolean = false): ApiResult<List<SymbolDto>> {
        cachedSymbols?.let { if (!forceRefresh) return ApiResult.Success(it) }
        return when (val r = ioCall { network.requireApi().getTradableSymbols() }) {
            is ApiResult.Success -> { cachedSymbols = r.data; r }
            else -> r
        }
    }

    // --- Write APIs (CSRF-guarded) ---------------------------------------

    suspend fun placeTrade(request: TradeRequest): ApiResult<TradeResponseDto> {
        val scope = adminState.scope.value
        val routed = request.copy(
            dashboardScope = scope.tradeScope(),
            dashboardTargetUserId = scope.tradeTargetUserId(),
        )
        return postCall { network.requireApi().proxyTrade(routed) }
    }

    /** Closes [symbol]; [ownerUserId] targets a specific user's position (admin). */
    suspend fun closePosition(symbol: String, ownerUserId: Long? = null): ApiResult<TradeResponseDto> {
        val target = if (adminState.isAdmin.value) ownerUserId else null
        val request = TradeRequest(
            symbol = symbol,
            action = "close",
            dashboardScope = "single",
            dashboardTargetUserId = target,
        )
        return postCall { network.requireApi().proxyTrade(request) }
    }

    suspend fun ask(question: String, symbols: String): ApiResult<IntelAnswerDto> =
        postCall {
            network.requireApi().ask(mapOf("question" to question, "symbols" to symbols))
        }

    suspend fun analysis(symbols: String): ApiResult<AnalysisResponseDto> =
        postCall {
            network.requireApi().analysis(mapOf("symbols" to symbols))
        }

    // --- Plumbing ---------------------------------------------------------

    private suspend fun <T> ioCall(call: suspend () -> Response<T>): ApiResult<T> =
        withContext(Dispatchers.IO) {
            try {
                mapJson(call())
            } catch (e: Exception) {
                ApiResult.Error(e.friendly())
            }
        }

    /** POST helper that transparently re-scrapes the CSRF token and retries once. */
    private suspend fun <T> postCall(call: suspend () -> Response<T>): ApiResult<T> =
        withContext(Dispatchers.IO) {
            try {
                if (session.csrfToken == null) refreshCsrf()
                var resp = call()
                var errBody = if (resp.isSuccessful) null else readError(resp)
                if (resp.code() == 400 && errBody?.contains("csrf_failed") == true) {
                    session.csrfToken = null
                    refreshCsrf()
                    resp = call()
                    errBody = if (resp.isSuccessful) null else readError(resp)
                }
                mapJson(resp, errBody)
            } catch (e: Exception) {
                ApiResult.Error(e.friendly())
            }
        }

    private fun <T> mapJson(resp: Response<T>, preReadError: String? = null): ApiResult<T> {
        if (resp.isSuccessful) {
            val body = resp.body()
            return if (body != null) ApiResult.Success(body)
            else ApiResult.Error("Empty response from server.", resp.code())
        }
        val code = resp.code()
        if (code in 300..399) {
            val location = resp.raw().header("Location").orEmpty()
            return if (location.contains("login", ignoreCase = true)) {
                ApiResult.Unauthorized
            } else {
                ApiResult.Error("Unexpected redirect from server.", code)
            }
        }
        val raw = preReadError ?: readError(resp)
        return ApiResult.Error(messageFromError(raw, code), code)
    }

    private fun readError(resp: Response<*>): String? =
        runCatching { resp.errorBody()?.string() }.getOrNull()

    private fun messageFromError(raw: String?, code: Int): String {
        if (raw.isNullOrBlank()) return "Request failed (HTTP $code)."
        // Try to decode the JSON error envelope; fall back to a trimmed snippet.
        val parsed = runCatching { errorAdapter.fromJson(raw) }.getOrNull()
        val detail = parsed?.detail ?: parsed?.message ?: parsed?.error
        return when {
            !detail.isNullOrBlank() -> detail
            raw.length <= 200 && !raw.trimStart().startsWith("<") -> raw.trim()
            else -> "Request failed (HTTP $code)."
        }
    }

    private fun extractCsrf(html: String?): String? {
        if (html == null) return null
        return CSRF_META.find(html)?.groupValues?.getOrNull(1)
    }

    private fun Throwable.friendly(): String =
        message?.takeIf { it.isNotBlank() } ?: this::class.java.simpleName

    suspend fun createUser(
        username: String,
        email: String,
        tvUser: String,
        password: String,
    ): ApiResult<String> = withContext(Dispatchers.IO) {
        try {
            if (session.csrfToken == null) refreshCsrf()
            val resp = network.requireApi().createUser(username, email, tvUser, password)
            if (resp.code() in 300..399) {
                val location = resp.raw().header("Location").orEmpty()
                if (location.contains("login", ignoreCase = true)) {
                    return@withContext ApiResult.Unauthorized
                }
                val page = network.requireApi().getAdminUsersPage()
                val html = page.body()?.string() ?: ""
                val success = FLASH_SUCCESS.find(html)?.groupValues?.getOrNull(1)?.trim()
                val danger = FLASH_DANGER.find(html)?.groupValues?.getOrNull(1)?.trim()
                return@withContext when {
                    !success.isNullOrBlank() -> ApiResult.Success(success)
                    !danger.isNullOrBlank() -> ApiResult.Error(danger)
                    else -> ApiResult.Error("Unknown response from server.")
                }
            }
            ApiResult.Error("Unexpected response (HTTP ${resp.code()}).")
        } catch (e: Exception) {
            ApiResult.Error(e.friendly())
        }
    }

    private companion object {
        val CSRF_META =
            Regex("""<meta\s+name=["']csrf-token["']\s+content=["']([^"']+)["']""", RegexOption.IGNORE_CASE)

        val ADMIN_USER_OPTION =
            Regex("""value="(\d+)"\s+data-per-trade-amount="[^"]*"[^>]*>\s*([^<]+?)\s*</option>""", RegexOption.IGNORE_CASE)

        val FLASH_SUCCESS = Regex("""alert-success[^>]*>\s*([^<]+)""", RegexOption.IGNORE_CASE)
        val FLASH_DANGER = Regex("""alert-danger[^>]*>\s*([^<]+)""", RegexOption.IGNORE_CASE)
    }
}
