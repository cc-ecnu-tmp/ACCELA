plugins {
    id("java")
    id("org.graalvm.buildtools.native") version "0.10.3"
}

group = "accela"
version = "1.0-SNAPSHOT"

repositories {
    mavenCentral()
}

dependencies {
    testImplementation(platform("org.junit:junit-bom:5.10.0"))
    testImplementation("org.junit.jupiter:junit-jupiter")
    testRuntimeOnly("org.junit.platform:junit-platform-launcher")
}

tasks.withType<Jar>(){
    manifest{
        attributes["Main-Class"] = "accela.Main"
    }
}

tasks.test {
    useJUnitPlatform()
}

graalvmNative {
    binaries {
        named("main") {
            imageName.set("accela")
            mainClass.set("accela.Main")
            buildArgs.add("--no-fallback")
        }
    }
}
