#define SDL_MAIN_HANDLED
// Tests SDL2 video init, WASAPI audio, and D3D11 hardware device inside an AppContainer.
// Exits 0 on PASS, 1 on FAIL. Prints one status line to stdout.
#include <SDL.h>
#include <d3d11.h>
#include <dxgi.h>
#include <cstdio>
#include <iostream>

int main() {
    SDL_SetMainReady();

    if (SDL_Init(SDL_INIT_VIDEO | SDL_INIT_AUDIO) != 0) {
        std::cout << "FAIL: SDL_Init: " << SDL_GetError() << "\n";
        return 1;
    }

    SDL_Window* win = SDL_CreateWindow(
        "sdl2_d3d11_check",
        SDL_WINDOWPOS_UNDEFINED, SDL_WINDOWPOS_UNDEFINED,
        320, 240,
        SDL_WINDOW_SHOWN
    );
    if (!win) {
        std::cout << "FAIL: SDL_CreateWindow: " << SDL_GetError() << "\n";
        SDL_Quit();
        return 1;
    }

    // WASAPI shared-mode audio
    SDL_AudioSpec want{}, have{};
    want.freq     = 44100;
    want.format   = AUDIO_F32SYS;
    want.channels = 2;
    want.samples  = 512;
    SDL_AudioDeviceID audio = SDL_OpenAudioDevice(nullptr, 0, &want, &have, 0);
    if (!audio) {
        std::cout << "FAIL: SDL_OpenAudioDevice (WASAPI): " << SDL_GetError() << "\n";
        SDL_DestroyWindow(win);
        SDL_Quit();
        return 1;
    }
    SDL_CloseAudioDevice(audio);

    // D3D11 hardware device
    ID3D11Device*        device = nullptr;
    ID3D11DeviceContext* ctx    = nullptr;
    D3D_FEATURE_LEVEL    level  = {};
    HRESULT hr = D3D11CreateDevice(
        nullptr,
        D3D_DRIVER_TYPE_HARDWARE,
        nullptr, 0,
        nullptr, 0,
        D3D11_SDK_VERSION,
        &device, &level, &ctx
    );
    if (FAILED(hr)) {
        char buf[32];
        snprintf(buf, sizeof(buf), "0x%08lX", static_cast<unsigned long>(hr));
        std::cout << "FAIL: D3D11CreateDevice hr=" << buf << "\n";
        SDL_DestroyWindow(win);
        SDL_Quit();
        return 1;
    }

    // Confirm the adapter is not the software (WARP) fallback
    IDXGIDevice*   dxgi_dev = nullptr;
    IDXGIAdapter*  adapter  = nullptr;
    IDXGIAdapter1* adapter1 = nullptr;
    bool is_software = false;

    if (SUCCEEDED(device->QueryInterface(__uuidof(IDXGIDevice), reinterpret_cast<void**>(&dxgi_dev)))) {
        if (SUCCEEDED(dxgi_dev->GetAdapter(&adapter))) {
            if (SUCCEEDED(adapter->QueryInterface(__uuidof(IDXGIAdapter1), reinterpret_cast<void**>(&adapter1)))) {
                DXGI_ADAPTER_DESC1 desc{};
                adapter1->GetDesc1(&desc);
                is_software = (desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) != 0;
                adapter1->Release();
            }
            adapter->Release();
        }
        dxgi_dev->Release();
    }

    if (ctx)    ctx->Release();
    if (device) device->Release();
    SDL_DestroyWindow(win);
    SDL_Quit();

    if (is_software) {
        std::cout << "FAIL: D3D11 adapter is software/WARP, GPU not accessible\n";
        return 1;
    }

    std::cout << "PASS: SDL2 init, WASAPI audio, and D3D11 hardware device all accessible\n";
    return 0;
}
