Okay, big change:

To approximate sigmoid, we've been using
\left\{x<-2:0,\ x<2:0.25x+0.5,\ 1\right\}
which is fine, but has a max error of 1.192029e-01

Here are better approximations:

1 segment — max error 0.056050
\left\{x<-2.823822:0,\ x<2.823822:0.177065x+0.5,\ 1\right\}

3 segments — max error 0.017376
\left\{x<-4.035162:0,\ x<-1.652934:0.060169x+0.242793,\ x<1.652934:0.215776x+0.5,\ x<4.035162:0.060169x+0.757207,\ 1\right\}

5 segments — max error 0.008362
\left\{x<-4.775714:0,\ x<-2.508140:0.029515x+0.140956,\ x<-1.243333:0.117462x+0.361539,\ x<1.243333:0.228825x+0.5,\ x<2.508140:0.117462x+0.638461,\ x<4.775714:0.029515x+0.859044,\ 1\right\}

Though if we only multiply by powers of 2, we get this:

1 segment — error 0.119203
\left\{x<-2:0,\ x<2:0.25x+0.5,\ 1\right\}

3 segments — error 0.034857, slopes 2^-4, 2^-2, 2^-4
\left\{x<-4.263425:0,\ x<-1.245525:0.0625x+0.266464,\ x<1.245525:0.25x+0.5,\ x<4.263425:0.0625x+0.733536,\ 1\right\}

5 segments — error 0.015848, slopes 2^-5, 2^-3, 2^-2, 2^-3, 2^-5
\left\{x<-4.565853:0,\ x<-2.559516:0.03125x+0.142683,\ x<-0.938899:0.125x+0.382638,\ x<0.938899:0.25x+0.5,\ x<2.559516:0.125x+0.617362,\ x<4.565853:0.03125x+0.857317,\ 1\right\}

We should never go past 5 segments. We can just hard-code these 7 variants, and use a SigmoidModel enum param to choose between
LUT
APPROXIMATE_1ST_ORDER
APPROXIMATE_3RD_ORDER
APPROXIMATE_5TH_ORDER
APPROXIMATE_POWER2_SLOPE_1ST_ORDER
APPROXIMATE_POWER2_SLOPE_3RD_ORDER
APPROXIMATE_POWER2_SLOPE_5TH_ORDER
