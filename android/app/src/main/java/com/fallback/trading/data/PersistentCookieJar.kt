package com.fallback.trading.data

import com.squareup.moshi.Moshi
import com.squareup.moshi.Types
import okhttp3.Cookie
import okhttp3.CookieJar
import okhttp3.HttpUrl
import java.util.concurrent.ConcurrentHashMap

/** Serializable snapshot of an [okhttp3.Cookie]. */
data class PersistedCookie(
    val name: String,
    val value: String,
    val expiresAt: Long,
    val domain: String,
    val path: String,
    val secure: Boolean,
    val httpOnly: Boolean,
    val hostOnly: Boolean,
)

/**
 * A [CookieJar] that keeps cookies in memory and mirrors them, encrypted, into
 * [SecureStore]. This is what keeps the Flask `session` cookie alive across app
 * restarts. Expired cookies are pruned lazily on read/write.
 */
class PersistentCookieJar(
    private val secureStore: SecureStore,
    moshi: Moshi,
) : CookieJar {

    private val listType = Types.newParameterizedType(List::class.java, PersistedCookie::class.java)
    private val adapter = moshi.adapter<List<PersistedCookie>>(listType)

    // key = name|domain|path
    private val cache = ConcurrentHashMap<String, Cookie>()

    init {
        runCatching {
            secureStore.getString(SecureStore.KEY_COOKIES)?.let { json ->
                adapter.fromJson(json)?.forEach { pc ->
                    val cookie = pc.toCookie()
                    if (cookie != null && cookie.expiresAt > System.currentTimeMillis()) {
                        cache[keyOf(cookie)] = cookie
                    }
                }
            }
        }
    }

    override fun saveFromResponse(url: HttpUrl, cookies: List<Cookie>) {
        val now = System.currentTimeMillis()
        for (cookie in cookies) {
            val key = keyOf(cookie)
            if (cookie.expiresAt <= now) cache.remove(key) else cache[key] = cookie
        }
        persist()
    }

    override fun loadForRequest(url: HttpUrl): List<Cookie> {
        val now = System.currentTimeMillis()
        val matches = ArrayList<Cookie>()
        var pruned = false
        val iterator = cache.entries.iterator()
        while (iterator.hasNext()) {
            val cookie = iterator.next().value
            if (cookie.expiresAt <= now) {
                iterator.remove()
                pruned = true
            } else if (cookie.matches(url)) {
                matches.add(cookie)
            }
        }
        if (pruned) persist()
        return matches
    }

    fun clear() {
        cache.clear()
        secureStore.putString(SecureStore.KEY_COOKIES, null)
    }

    private fun persist() {
        val snapshot = cache.values.map { it.toPersisted() }
        secureStore.putString(SecureStore.KEY_COOKIES, adapter.toJson(snapshot))
    }

    private fun keyOf(cookie: Cookie) = "${cookie.name}|${cookie.domain}|${cookie.path}"

    private fun Cookie.toPersisted() = PersistedCookie(
        name = name,
        value = value,
        expiresAt = expiresAt,
        domain = domain,
        path = path,
        secure = secure,
        httpOnly = httpOnly,
        hostOnly = hostOnly,
    )

    private fun PersistedCookie.toCookie(): Cookie? {
        val builder = Cookie.Builder()
            .name(name)
            .value(value)
            .path(path)
            .expiresAt(expiresAt)
        if (hostOnly) builder.hostOnlyDomain(domain) else builder.domain(domain)
        if (secure) builder.secure()
        if (httpOnly) builder.httpOnly()
        return runCatching { builder.build() }.getOrNull()
    }
}
