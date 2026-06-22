package com.fallback.trading

import android.app.Application
import android.content.Context
import com.fallback.trading.data.AdminState
import com.fallback.trading.data.NetworkClient
import com.fallback.trading.data.NotificationHelper
import com.fallback.trading.data.PersistentCookieJar
import com.fallback.trading.data.SecureStore
import com.fallback.trading.data.SessionState
import com.fallback.trading.data.SettingsStore
import com.fallback.trading.data.TradingRepository
import com.fallback.trading.data.UpdateManager

/** Manual dependency container — small enough not to need Hilt. */
class AppContainer(context: Context) {
    val appContext: Context = context.applicationContext
    private val moshi = NetworkClient.buildMoshi()

    val settings = SettingsStore(appContext)
    private val secureStore = SecureStore(appContext)
    private val cookieJar = PersistentCookieJar(secureStore, moshi)
    private val session = SessionState()
    private val network = NetworkClient(cookieJar, session, moshi)
    private val adminState = AdminState()

    val repository = TradingRepository(network, session, cookieJar, settings, secureStore, adminState, moshi)
    val updateManager = UpdateManager(appContext, moshi)
}

class TradingApp : Application() {
    lateinit var container: AppContainer
        private set

    override fun onCreate() {
        super.onCreate()
        container = AppContainer(this)
        NotificationHelper.createChannel(this)
    }
}
