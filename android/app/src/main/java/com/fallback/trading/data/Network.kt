package com.fallback.trading.data

import com.fallback.trading.BuildConfig
import com.squareup.moshi.Moshi
import com.squareup.moshi.kotlin.reflect.KotlinJsonAdapterFactory
import okhttp3.Interceptor
import okhttp3.OkHttpClient
import okhttp3.Response
import okhttp3.ResponseBody
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.moshi.MoshiConverterFactory
import retrofit2.http.Body
import retrofit2.http.Field
import retrofit2.http.FormUrlEncoded
import retrofit2.http.GET
import retrofit2.http.POST
import java.util.concurrent.TimeUnit

/** Holds the current CSRF token in memory; refreshed after login / on demand. */
class SessionState {
    @Volatile
    var csrfToken: String? = null
}

/** Adds the X-CSRF-Token header to every state-changing request (matches base.html). */
class CsrfInterceptor(private val session: SessionState) : Interceptor {
    override fun intercept(chain: Interceptor.Chain): Response {
        val request = chain.request()
        val method = request.method.uppercase()
        val needsCsrf = method !in SAFE_METHODS
        val token = session.csrfToken
        return if (needsCsrf && token != null) {
            chain.proceed(request.newBuilder().header("X-CSRF-Token", token).build())
        } else {
            chain.proceed(request)
        }
    }

    private companion object {
        val SAFE_METHODS = setOf("GET", "HEAD", "OPTIONS", "TRACE")
    }
}

interface TradingApi {
    @GET("login")
    suspend fun getLoginPage(): retrofit2.Response<ResponseBody>

    @FormUrlEncoded
    @POST("login")
    suspend fun postLogin(
        @Field("username") username: String,
        @Field("password") password: String,
        @Field("csrf_token") csrfToken: String,
    ): retrofit2.Response<ResponseBody>

    @GET(".")
    suspend fun getRoot(): retrofit2.Response<ResponseBody>

    @POST("logout")
    suspend fun postLogout(): retrofit2.Response<ResponseBody>

    @GET("api/account")
    suspend fun getAccount(
        @retrofit2.http.Query("dashboard_scope") scope: String? = null,
        @retrofit2.http.Query("user_id") userId: Long? = null,
    ): retrofit2.Response<AccountDto>

    @GET("api/open_positions")
    suspend fun getOpenPositions(
        @retrofit2.http.Query("dashboard_scope") scope: String? = null,
        @retrofit2.http.Query("user_id") userId: Long? = null,
    ): retrofit2.Response<List<PositionDto>>

    @GET("api/closed_orders")
    suspend fun getClosedOrders(
        @retrofit2.http.Query("user_id") userId: Long? = null,
    ): retrofit2.Response<List<ClosedTradeDto>>

    @GET("api/tradable_symbols")
    suspend fun getTradableSymbols(): retrofit2.Response<List<SymbolDto>>

    @GET("api/admin/dashboard_summary")
    suspend fun getAdminSummary(): retrofit2.Response<List<AdminUserSummaryDto>>

    @GET("api/admin/performance_leaderboard")
    suspend fun getLeaderboard(): retrofit2.Response<List<LeaderboardEntryDto>>

    @POST("api/proxy_trade")
    suspend fun proxyTrade(@Body request: TradeRequest): retrofit2.Response<TradeResponseDto>

    @POST("api/stock_intelligence/ask")
    suspend fun ask(@Body body: Map<String, String>): retrofit2.Response<IntelAnswerDto>

    @POST("api/stock_intelligence/analysis")
    suspend fun analysis(@Body body: Map<String, String>): retrofit2.Response<AnalysisResponseDto>

    @FormUrlEncoded
    @POST("admin/users/create")
    suspend fun createUser(
        @Field("username") username: String,
        @Field("email") email: String,
        @Field("tradingview_user") tvUser: String,
        @Field("password") password: String,
    ): retrofit2.Response<ResponseBody>

    @GET("admin/users")
    suspend fun getAdminUsersPage(): retrofit2.Response<ResponseBody>
}

/**
 * Owns the [OkHttpClient] (built once, with the persistent cookie jar) and a
 * [TradingApi] that is rebuilt whenever the server base URL changes.
 *
 * Automatic redirects are disabled so the repository can distinguish a real
 * JSON response from a 302 bounce to /login (session expired).
 */
class NetworkClient(
    cookieJar: PersistentCookieJar,
    session: SessionState,
    private val moshi: Moshi,
) {
    private val client: OkHttpClient = OkHttpClient.Builder()
        .cookieJar(cookieJar)
        .addInterceptor(CsrfInterceptor(session))
        .apply {
            if (BuildConfig.DEBUG) {
                addInterceptor(HttpLoggingInterceptor().apply {
                    level = HttpLoggingInterceptor.Level.BASIC
                })
            }
        }
        .followRedirects(false)
        .followSslRedirects(false)
        .connectTimeout(20, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)   // LLM endpoints can be slow
        .writeTimeout(30, TimeUnit.SECONDS)
        .build()

    @Volatile
    var baseUrl: String? = null
        private set

    @Volatile
    private var api: TradingApi? = null

    fun setBaseUrl(url: String) {
        if (url == baseUrl && api != null) return
        baseUrl = url
        api = Retrofit.Builder()
            .baseUrl(url)
            .client(client)
            .addConverterFactory(MoshiConverterFactory.create(moshi))
            .build()
            .create(TradingApi::class.java)
    }

    fun requireApi(): TradingApi =
        api ?: error("Server URL has not been configured yet.")

    companion object {
        fun buildMoshi(): Moshi = Moshi.Builder()
            .add(KotlinJsonAdapterFactory())
            .build()
    }
}
