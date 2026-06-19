package com.fallback.trading.ui.theme

import android.app.Activity
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.SideEffect
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.unit.dp
import androidx.core.view.WindowCompat

// --- Brand palette (eToro-inspired) ---------------------------------------
val BrandGreen = Color(0xFF00C281)
val BrandTeal = Color(0xFF13C2C2)
val BrandBlue = Color(0xFF3D7BFF)
val ProfitGreen = Color(0xFF16C784)
val LossRed = Color(0xFFEA3943)

private val Ink = Color(0xFF0B0F14)
private val InkSurface = Color(0xFF151B23)
private val InkSurfaceVariant = Color(0xFF1E2733)
private val InkOutline = Color(0xFF2A3543)
private val TextHigh = Color(0xFFE6EDF3)
private val TextMuted = Color(0xFF8B97A7)

private val DarkColors = darkColorScheme(
    primary = BrandGreen,
    onPrimary = Color(0xFF04130D),
    primaryContainer = Color(0xFF0C3A2A),
    onPrimaryContainer = Color(0xFFB9F5DC),
    secondary = BrandBlue,
    onSecondary = Color.White,
    tertiary = BrandTeal,
    background = Ink,
    onBackground = TextHigh,
    surface = InkSurface,
    onSurface = TextHigh,
    surfaceVariant = InkSurfaceVariant,
    onSurfaceVariant = TextMuted,
    surfaceContainer = InkSurface,
    surfaceContainerHigh = InkSurfaceVariant,
    outline = InkOutline,
    outlineVariant = Color(0xFF22303D),
    error = LossRed,
    onError = Color.White,
)

private val LightColors = lightColorScheme(
    primary = Color(0xFF00A36C),
    onPrimary = Color.White,
    secondary = BrandBlue,
    tertiary = BrandTeal,
    background = Color(0xFFF4F7FB),
    onBackground = Color(0xFF0F172A),
    surface = Color.White,
    onSurface = Color(0xFF0F172A),
    surfaceVariant = Color(0xFFE9EEF5),
    onSurfaceVariant = Color(0xFF566173),
    error = LossRed,
)

val AppShapes = Shapes(
    extraSmall = RoundedCornerShape(8.dp),
    small = RoundedCornerShape(12.dp),
    medium = RoundedCornerShape(18.dp),
    large = RoundedCornerShape(24.dp),
    extraLarge = RoundedCornerShape(32.dp),
)

val AppTypography = Typography()

/** Signed P/L color helper. */
fun plColor(value: Double): Color = if (value >= 0) ProfitGreen else LossRed

/** The hero/accent gradient used on the portfolio header and the trade FAB. */
@Composable
fun brandGradient(): Brush = Brush.linearGradient(listOf(BrandGreen, BrandTeal, BrandBlue))

@Composable
fun FallbackTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit,
) {
    val colorScheme = if (darkTheme) DarkColors else LightColors

    val view = LocalView.current
    if (!view.isInEditMode) {
        SideEffect {
            val window = (view.context as Activity).window
            WindowCompat.getInsetsController(window, view).isAppearanceLightStatusBars = !darkTheme
        }
    }

    MaterialTheme(
        colorScheme = colorScheme,
        typography = AppTypography,
        shapes = AppShapes,
        content = content,
    )
}
