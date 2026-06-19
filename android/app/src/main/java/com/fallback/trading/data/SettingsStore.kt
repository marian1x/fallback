package com.fallback.trading.data

import android.content.Context
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map

private val Context.dataStore by preferencesDataStore(name = "settings")

/**
 * Non-secret preferences: the server base URL and the last username (for prefill).
 * Session cookies and any secrets live in [SecureStore] instead.
 */
class SettingsStore(private val context: Context) {

    private object Keys {
        val BASE_URL = stringPreferencesKey("base_url")
        val LAST_USERNAME = stringPreferencesKey("last_username")
    }

    val baseUrl: Flow<String?> = context.dataStore.data.map { it[Keys.BASE_URL] }
    val lastUsername: Flow<String?> = context.dataStore.data.map { it[Keys.LAST_USERNAME] }

    suspend fun baseUrlOnce(): String? = baseUrl.first()

    suspend fun setBaseUrl(url: String) {
        context.dataStore.edit { it[Keys.BASE_URL] = url }
    }

    suspend fun setLastUsername(username: String) {
        context.dataStore.edit { it[Keys.LAST_USERNAME] = username }
    }

    companion object {
        /** Normalize user input into a valid Retrofit base URL (scheme present, trailing slash). */
        fun normalizeBaseUrl(raw: String): String? {
            var s = raw.trim()
            if (s.isEmpty()) return null
            if (!s.startsWith("http://", true) && !s.startsWith("https://", true)) {
                s = "https://$s"
            }
            if (!s.endsWith("/")) s = "$s/"
            return s
        }
    }
}
