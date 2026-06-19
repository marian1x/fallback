# Retrofit / OkHttp / Moshi
-dontwarn okhttp3.**
-dontwarn okio.**
-dontwarn javax.annotation.**
-keepattributes Signature, RuntimeVisibleAnnotations, AnnotationDefault

# Retrofit service interfaces
-keep,allowobfuscation,allowshrinking interface retrofit2.Call
-keep,allowobfuscation,allowshrinking class retrofit2.Response
-keepclasseswithmembers class * {
    @retrofit2.http.* <methods>;
}

# Moshi-reflective models: keep the data classes used for (de)serialization.
-keep class com.fallback.trading.data.** { *; }
-keepclassmembers class com.fallback.trading.data.** { *; }

# Tink (used by androidx.security.crypto / EncryptedSharedPreferences) references
# compile-only ErrorProne annotations that aren't present at runtime.
-dontwarn com.google.errorprone.annotations.**
-dontwarn javax.annotation.**

# Moshi reflective adapter internals.
-keep class kotlin.reflect.jvm.internal.** { *; }
-dontwarn org.jetbrains.annotations.**
