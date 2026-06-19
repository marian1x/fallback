package com.fallback.trading.ui

import com.fallback.trading.data.ApiResult

/** Generic screen state for a single async resource. */
data class UiState<T>(
    val loading: Boolean = false,
    val data: T? = null,
    val error: String? = null,
    val sessionExpired: Boolean = false,
)

/** Reduce an [ApiResult] into a [UiState], preserving previously loaded data on error. */
fun <T> UiState<T>.reduce(result: ApiResult<T>): UiState<T> = when (result) {
    is ApiResult.Success -> UiState(data = result.data)
    is ApiResult.Unauthorized -> copy(loading = false, sessionExpired = true)
    is ApiResult.Error -> copy(loading = false, error = result.message)
}
