package com.fallback.trading.ui.components

import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.clipRect
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import com.fallback.trading.ui.Format
import kotlin.math.abs
import kotlin.math.min

/**
 * Animated area/line chart drawn directly on a Canvas. Designed for a portfolio
 * P/L curve — handles negative values and animates the line drawing left→right.
 */
@Composable
fun LineChart(
    values: List<Float>,
    modifier: Modifier = Modifier,
    lineColor: Color = MaterialTheme.colorScheme.primary,
) {
    val progress = remember(values) { Animatable(0f) }
    LaunchedEffect(values) { progress.animateTo(1f, tween(900)) }
    val fillTop = lineColor.copy(alpha = 0.30f)

    Canvas(modifier) {
        if (values.size < 2) {
            val midY = size.height / 2f
            drawLine(
                color = lineColor.copy(alpha = 0.35f),
                start = Offset(0f, midY),
                end = Offset(size.width, midY),
                strokeWidth = 3.dp.toPx(),
                cap = StrokeCap.Round,
            )
            return@Canvas
        }

        val minV = values.min()
        val maxV = values.max()
        val range = (maxV - minV).takeIf { it > 0f } ?: 1f
        val stepX = size.width / (values.size - 1)
        val pad = size.height * 0.12f
        fun y(v: Float): Float = pad + (1f - (v - minV) / range) * (size.height - 2 * pad)

        val line = Path()
        val fill = Path().apply { moveTo(0f, size.height) }
        values.forEachIndexed { i, v ->
            val px = i * stepX
            val py = y(v)
            if (i == 0) line.moveTo(px, py) else line.lineTo(px, py)
            fill.lineTo(px, py)
        }
        fill.lineTo(size.width, size.height)
        fill.close()

        clipRect(right = size.width * progress.value) {
            drawPath(fill, brush = Brush.verticalGradient(listOf(fillTop, Color.Transparent)))
            drawPath(
                line,
                color = lineColor,
                style = Stroke(width = 3.dp.toPx(), cap = StrokeCap.Round, join = StrokeJoin.Round),
            )
        }

        // Marker dot at the current animated end of the line.
        val idx = progress.value * (values.size - 1)
        val i0 = idx.toInt().coerceIn(0, values.size - 1)
        val i1 = (i0 + 1).coerceAtMost(values.size - 1)
        val frac = idx - i0
        val v = values[i0] + (values[i1] - values[i0]) * frac
        val dot = Offset(idx * stepX, y(v))
        drawCircle(lineColor.copy(alpha = 0.20f), radius = 7.dp.toPx(), center = dot)
        drawCircle(lineColor, radius = 4.dp.toPx(), center = dot)
    }
}

data class BarEntry(val label: String, val value: Float)

/** Labeled horizontal bar chart. Supports positive (green) and negative (red) values. */
@Composable
fun HorizontalBarChart(
    entries: List<BarEntry>,
    modifier: Modifier = Modifier,
    barColor: Color = MaterialTheme.colorScheme.primary,
    positiveColor: Color = barColor,
    negativeColor: Color = barColor,
) {
    if (entries.isEmpty()) return
    val max = entries.maxOfOrNull { abs(it.value) }?.takeIf { it > 0f } ?: 1f
    Column(modifier = modifier, verticalArrangement = Arrangement.spacedBy(6.dp)) {
        entries.forEach { entry ->
            val fraction = (abs(entry.value) / max).coerceIn(0f, 1f)
            val color = if (entry.value >= 0f) positiveColor else negativeColor
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                Text(
                    entry.label,
                    modifier = Modifier.width(72.dp),
                    style = MaterialTheme.typography.labelSmall,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                Box(
                    modifier = Modifier
                        .weight(1f)
                        .height(14.dp)
                        .clip(RoundedCornerShape(4.dp))
                        .background(MaterialTheme.colorScheme.surfaceVariant),
                ) {
                    Box(
                        modifier = Modifier
                            .fillMaxHeight()
                            .fillMaxWidth(fraction)
                            .clip(RoundedCornerShape(4.dp))
                            .background(color.copy(alpha = 0.85f)),
                    )
                }
                Text(
                    Format.moneySigned(entry.value.toDouble()),
                    modifier = Modifier.width(64.dp),
                    style = MaterialTheme.typography.labelSmall,
                    textAlign = TextAlign.End,
                    maxLines = 1,
                )
            }
        }
    }
}

data class DonutSlice(val label: String, val value: Float, val color: Color)

/** Animated allocation donut. */
@Composable
fun DonutChart(
    slices: List<DonutSlice>,
    modifier: Modifier = Modifier,
    strokeWidth: Dp = 18.dp,
) {
    val total = slices.sumOf { it.value.toDouble() }.toFloat().takeIf { it > 0f } ?: 1f
    val progress = remember(slices) { Animatable(0f) }
    LaunchedEffect(slices) { progress.animateTo(1f, tween(800)) }

    Canvas(modifier) {
        val sw = strokeWidth.toPx()
        val diameter = min(size.width, size.height) - sw
        val topLeft = Offset((size.width - diameter) / 2f, (size.height - diameter) / 2f)
        val arcSize = Size(diameter, diameter)
        var start = -90f
        slices.forEach { slice ->
            val fullSweep = (slice.value / total) * 360f
            val sweep = (fullSweep * progress.value - 3f).coerceAtLeast(0f)
            drawArc(
                color = slice.color,
                startAngle = start,
                sweepAngle = sweep,
                useCenter = false,
                topLeft = topLeft,
                size = arcSize,
                style = Stroke(width = sw, cap = StrokeCap.Round),
            )
            start += fullSweep
        }
    }
}
