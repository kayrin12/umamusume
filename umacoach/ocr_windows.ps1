# Persistent, local-only Windows OCR. No execution-policy override or downloads.
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
    $null = [Windows.Storage.FileAccessMode, Windows.Storage, ContentType=WindowsRuntime]
    $null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
    $null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType=WindowsRuntime]
    $awaitMethod = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetGenericArguments().Count -eq 1 -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } | Select-Object -First 1
    function Await-Result($operation, $resultType) {
        $task = $awaitMethod.MakeGenericMethod($resultType).Invoke($null, @($operation))
        $null = $task.Wait()
        return $task.Result
    }
    $engine = $null
    $languages = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages)
    foreach ($language in ($languages | Sort-Object @{Expression={if($_.LanguageTag -match '^zh-(Hant|TW|HK)'){0}elseif($_.LanguageTag -match '^zh'){1}elseif($_.LanguageTag -match '^ja'){2}else{3}}})) {
        if ($language.LanguageTag -match '^(zh|ja)') {
            $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
            if ($null -ne $engine) { break }
        }
    }
    if ($null -eq $engine) {
        @{ready=$false; error='language_missing'} | ConvertTo-Json -Compress
        exit 0
    }
    @{ready=$true; language=$engine.RecognizerLanguage.LanguageTag} | ConvertTo-Json -Compress
    while ($null -ne ($line = [Console]::ReadLine())) {
        try {
            $job = $line | ConvertFrom-Json
            $items = @()
            foreach ($item in $job.items) {
                $stream = $null; $bitmap = $null; $converted = $null
                try {
                    $file = Await-Result ([Windows.Storage.StorageFile]::GetFileFromPathAsync([string]$item.path)) ([Windows.Storage.StorageFile])
                    $stream = Await-Result ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
                    $decoder = Await-Result ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
                    $bitmap = Await-Result ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
                    $converted = [Windows.Graphics.Imaging.SoftwareBitmap]::Convert($bitmap, [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8, [Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied)
                    $result = Await-Result ($engine.RecognizeAsync($converted)) ([Windows.Media.Ocr.OcrResult])
                    $words = @()
                    foreach ($textLine in $result.Lines) {
                        foreach ($word in $textLine.Words) {
                            $box = $word.BoundingRect
                            $words += @{text=$word.Text; x=$box.X; y=$box.Y; w=$box.Width; h=$box.Height}
                        }
                    }
                    $items += @{key=$item.key; text=$result.Text; words=$words; width=$converted.PixelWidth; height=$converted.PixelHeight}
                } finally {
                    if ($null -ne $converted) { $converted.Dispose() }
                    if ($null -ne $bitmap) { $bitmap.Dispose() }
                    if ($null -ne $stream) { $stream.Dispose() }
                }
            }
            @{ok=$true; items=$items} | ConvertTo-Json -Compress -Depth 8
        } catch {
            @{ok=$false; error='recognition_failed'} | ConvertTo-Json -Compress
        }
    }
} catch {
    @{ready=$false; error='ocr_unavailable'} | ConvertTo-Json -Compress
}
