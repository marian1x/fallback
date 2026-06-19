package com.fallback.trading.data

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow

data class AdminUser(val id: Long, val username: String)

/**
 * Trading "scope" — mirrors the web dashboard's admin routing control. A normal
 * user is always [Self]; a superuser can switch between all users, the pooled
 * account, or one specific user.
 */
sealed interface TradingScope {
    data object Self : TradingScope
    data object AllUsers : TradingScope
    data object Pool : TradingScope
    data class User(val id: Long, val username: String) : TradingScope

    val label: String
        get() = when (this) {
            Self -> "My account"
            AllUsers -> "All users"
            Pool -> "Pooled account"
            is User -> username
        }
}

/** Query params for the GET /api/account endpoint. */
fun TradingScope.accountParams(): Pair<String?, Long?> = when (this) {
    TradingScope.Self -> null to null
    TradingScope.AllUsers -> "all_users" to null
    TradingScope.Pool -> "pool" to null
    is TradingScope.User -> null to id
}

/** Query params for GET /api/open_positions. */
fun TradingScope.positionsParams(): Pair<String?, Long?> = when (this) {
    TradingScope.Self, TradingScope.AllUsers -> null to null
    TradingScope.Pool -> "pool" to null
    is TradingScope.User -> null to id
}

fun TradingScope.closedUserId(): Long? = (this as? TradingScope.User)?.id

/** Payload values for POST /api/proxy_trade. */
fun TradingScope.tradeScope(): String = when (this) {
    TradingScope.AllUsers -> "all_users"
    TradingScope.Pool -> "pool"
    else -> "single"
}

fun TradingScope.tradeTargetUserId(): Long? = (this as? TradingScope.User)?.id

/**
 * Shared, app-wide admin state. Populated by scraping the dashboard page after
 * login; read by every scope-aware repository call and by the UI scope selector.
 */
class AdminState {
    private val _isAdmin = MutableStateFlow(false)
    val isAdmin = _isAdmin.asStateFlow()

    private val _users = MutableStateFlow<List<AdminUser>>(emptyList())
    val users = _users.asStateFlow()

    private val _scope = MutableStateFlow<TradingScope>(TradingScope.Self)
    val scope = _scope.asStateFlow()

    fun setScope(scope: TradingScope) { _scope.value = scope }

    fun update(isAdmin: Boolean, users: List<AdminUser>) {
        _isAdmin.value = isAdmin
        _users.value = users
        // Default an admin to the aggregate view; a normal user only ever sees self.
        _scope.value = if (isAdmin) TradingScope.AllUsers else TradingScope.Self
    }

    fun reset() {
        _isAdmin.value = false
        _users.value = emptyList()
        _scope.value = TradingScope.Self
    }
}
