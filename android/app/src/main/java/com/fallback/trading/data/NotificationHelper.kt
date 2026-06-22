package com.fallback.trading.data

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import androidx.core.app.NotificationCompat
import com.fallback.trading.R
import com.fallback.trading.ui.Format

object NotificationHelper {
    const val CHANNEL_TRADES = "trade_events"

    fun createChannel(context: Context) {
        val channel = NotificationChannel(
            CHANNEL_TRADES,
            "Trade Events",
            NotificationManager.IMPORTANCE_DEFAULT,
        ).apply {
            description = "Alerts when trades are opened or closed"
        }
        context.getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
    }

    fun notifyOpened(context: Context, symbol: String, side: String, qty: Double, price: Double) {
        val label = if (side.equals("buy", ignoreCase = true)) "LONG" else "SHORT"
        post(
            context,
            id = "open_$symbol".hashCode(),
            title = "Trade Opened · $symbol",
            body = "$label ${Format.qty(qty)} @ ${Format.money(price)}",
        )
    }

    fun notifyClosed(context: Context, symbol: String, pl: Double? = null, plPct: Double? = null) {
        val body = if (pl != null) {
            val pct = if (plPct != null) " (${Format.percentSigned(plPct)})" else ""
            "P/L: ${Format.moneySigned(pl)}$pct"
        } else {
            "Position closed"
        }
        post(
            context,
            id = "close_$symbol".hashCode(),
            title = "Trade Closed · $symbol",
            body = body,
        )
    }

    private fun post(context: Context, id: Int, title: String, body: String) {
        val nm = context.getSystemService(NotificationManager::class.java)
        val notification = NotificationCompat.Builder(context, CHANNEL_TRADES)
            .setSmallIcon(R.drawable.ic_stat_notify)
            .setContentTitle(title)
            .setContentText(body)
            .setAutoCancel(true)
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .build()
        nm.notify(id, notification)
    }
}
