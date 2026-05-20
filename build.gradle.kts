plugins {
    id("java")
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
        attributes["Main-Class"] = "Compiler"
    }
}

tasks.test {
    useJUnitPlatform()
}
