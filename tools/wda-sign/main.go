// wda-sign re-signs an app (nested .xctest included) with a P12 and a
// provisioning profile, through the same go-ios signer `ios sign app` uses
// (signing.SignWithFiles). It exists because `ios sign app` looks the phone up
// on usbmux before it signs, so it fails with the phone on Wi-Fi only, while
// signing itself needs no phone. SideTap installs the result through the tunnel.
//
//	wda-sign --path=<ipa|app> --output=<signed ipa> --p12file=<p12> --profile=<mobileprovision>
//	         [--p12password=<password>] [--bundleid=<bundle id>]
package main

import (
	"flag"
	"fmt"
	"os"

	"github.com/danielpaulus/go-ios/ios/signing"
)

func main() {
	path := flag.String("path", "", "app or .ipa to sign")
	output := flag.String("output", "", "where to write the signed app")
	p12 := flag.String("p12file", "", "signing identity (.p12)")
	password := flag.String("p12password", "", "password of the .p12")
	profile := flag.String("profile", "", "provisioning profile (.mobileprovision)")
	bundleID := flag.String("bundleid", "", "bundle id to sign as (default: the app's own)")
	flag.Parse()

	result, err := signing.SignWithFiles(signing.SignWithFilesOptions{
		AppPath:     *path,
		OutputPath:  *output,
		BundleID:    *bundleID,
		P12Path:     *p12,
		P12Password: *password,
		ProfilePath: *profile,
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "failed signing app: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("signed %s as %s\n", result.OutputPath, result.BundleID)
}
