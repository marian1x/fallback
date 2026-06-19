package com.fallback.trading.ui

import java.text.NumberFormat
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale

/** Formatting helpers shared across screens. */
object Format {
    private val currency: NumberFormat = NumberFormat.getCurrencyInstance(Locale.US)
    private val dateTime: DateTimeFormatter =
        DateTimeFormatter.ofPattern("MMM d, yyyy HH:mm").withZone(ZoneId.systemDefault())

    fun money(value: Double?): String = currency.format(value ?: 0.0)

    fun moneySigned(value: Double?): String {
        val v = value ?: 0.0
        val sign = if (v > 0) "+" else ""
        return sign + currency.format(v)
    }

    fun percentSigned(value: Double?): String {
        val v = value ?: 0.0
        val sign = if (v > 0) "+" else ""
        return String.format(Locale.US, "%s%.2f%%", sign, v)
    }

    fun qty(value: Double): String =
        if (value == value.toLong().toDouble()) value.toLong().toString()
        else String.format(Locale.US, "%.4f", value)

    fun dateTime(iso: String?): String {
        if (iso.isNullOrBlank()) return "—"
        return try {
            dateTime.format(Instant.parse(iso))
        } catch (e: Exception) {
            try {
                // Some timestamps include an explicit offset rather than Z.
                dateTime.format(java.time.OffsetDateTime.parse(iso).toInstant())
            } catch (e2: Exception) {
                iso
            }
        }
    }
}
